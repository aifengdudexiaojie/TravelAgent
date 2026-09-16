"""手动联调：自建的「网页扫码登录」驱动器（会真的启动浏览器访问小红书）

用法：
  # ① 只验证"能实时出码"（无需手机）：空目录 → 应拿到 state=qr + 一张 PNG
  python test/manual/manual_live_login.py --mode qr

  # ② 验证"我们写出的 cookies.json 能被 MCP 接受"（round-trip，最重要）
  #    用一个放有有效登录态的目录 → 应判定 logged_in，写回 v2 格式，
  #    再把它交给真正的 MCP 起浏览器，check_login_status 必须回"已登录"
  python test/manual/manual_live_login.py --mode roundtrip --cookie <有效的 cookies.json>

  # ③ 真正的扫码流程（需要手机，服务器上人工验）
  python test/manual/manual_live_login.py --mode scan
      → 会打印/保存二维码 PNG，手机扫码并在手机上确认，脚本每 3 秒探测一次，
        直到 state=logged_in（或 4 分钟超时）。

注意：pytest 不收集 test/manual/（见 pytest.ini），不会在 CI 里跑。
"""
import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from services import xhs_login_browser as lb          # noqa: E402

MCP_EXE = ROOT / "xiaohongshumcp" / "xiaohongshu-mcp-windows-amd64.exe"
MCP_PORT = 18260


def save_png(b64: str, name: str) -> Path:
    out = ROOT / name
    out.write_bytes(base64.b64decode(b64 + "=" * (-len(b64) % 4)))
    return out


def probe_loop(user_id: str, timeout: float, save_as: str | None = None):
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        data = lb.probe(user_id)
        state = data.get("state")
        if state != last_state:
            print(f"  [{time.strftime('%H:%M:%S')}] state={state} {data.get('message', '')}")
            last_state = state
        if data.get("image_base64") and save_as:
            p = save_png(data["image_base64"], save_as)
            print(f"    二维码已保存：{p.name}（{p.stat().st_size} 字节）")
        if state == "logged_in":
            return data
        if state in ("error", "closed", "unavailable"):
            return data
        time.sleep(3)
    return {"state": "timeout"}


def verify_with_mcp(cookie_file: Path) -> bool:
    """把我们的 cookies.json 交给真正的 MCP，看它认不认。"""
    workdir = ROOT / "_manual_mcp_check"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    shutil.copy2(cookie_file, workdir / "cookies.json")
    log = (workdir / "mcp.log").open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([str(MCP_EXE), f"-port=:{MCP_PORT}", "-headless=true"],
                            cwd=str(workdir), stdout=log, stderr=log)
    try:
        import httpx
        from xiaohongshu_mcp_client import _batch_call
        base = f"http://localhost:{MCP_PORT}/mcp"
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                httpx.get(base, timeout=2)
                break
            except Exception:
                time.sleep(1)
        else:
            print("  ❌ MCP 没起来")
            return False
        text = _batch_call("tools/call", {"name": "check_login_status"}, base_url=base,
                           max_retries=0, timeout=120)
        text = json.dumps(text, ensure_ascii=False) if isinstance(text, dict) else str(text)
        print("  MCP 读我们写的 cookies.json →", text.strip().replace("\n", " / ")[:120])
        return "已登录" in text
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log.close()
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["qr", "roundtrip", "scan"], default="qr")
    ap.add_argument("--cookie", help="roundtrip 模式用的有效 cookies.json")
    ap.add_argument("--timeout", type=float, default=240)
    args = ap.parse_args()

    avail = lb.availability()
    print("playwright:", "✅" if avail["ok"] else f"❌ {avail['reason']}")
    print("复用浏览器:", avail["browser"] or "(playwright 自带)")
    if not avail["ok"]:
        return 1

    workdir = ROOT / "_manual_login_workdir"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    user_id = "manual-live-login"

    if args.mode == "roundtrip":
        src = Path(args.cookie) if args.cookie else (ROOT / "xiaohongshumcp" / "cookies.json")
        if not src.exists():
            print("找不到 cookie 文件：", src)
            return 1
        shutil.copy2(src, workdir / "cookies.json")
        print(f"\n== roundtrip：先放入 {src.name}（{src.stat().st_size} 字节）==")

    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            lb, "_workdir", lambda _u: workdir):
        started = lb.start(user_id)
        print(f"\nstart() → state={started.get('state')} {started.get('message', '')}")
        if started.get("image_base64"):
            p = save_png(started["image_base64"], "_live_qr.png")
            print(f"  二维码：{p.name}（{p.stat().st_size} 字节）")

        # 已有有效登录态时，start() 会直接判定 logged_in 并**主动结束会话**（无需扫码），
        # 所以这时不能再 probe（会话已关闭，属正常）。
        if started.get("state") == "logged_in":
            saved = workdir / "cookies.json"
            raw = json.loads(saved.read_text(encoding="utf-8"))
            print(f"  已写回 {saved.name}：version={raw.get('version')} "
                  f"seed={raw.get('seed')} cookies={len(raw.get('cookies') or [])}")
            ok = True
            if args.mode == "roundtrip":
                print("\n== 交给真正的 MCP 验证 ==")
                ok = verify_with_mcp(saved)
        elif args.mode == "qr":
            # 验证"每次探测都拿到当前这张码"（实时性）
            time.sleep(3)
            again = lb.probe(user_id)
            same = again.get("image_base64") == started.get("image_base64")
            print(f"  3 秒后再探测：state={again.get('state')}，"
                  f"图片{'内容相同（同一张码仍在有效期内）' if same else '已变化（页面换了新码，正好说明实时读取有效）'}")
            ok = started.get("state") == "qr" and bool(started.get("image_base64"))
        elif args.mode == "roundtrip":
            data = probe_loop(user_id, timeout=30)
            ok = data.get("state") == "logged_in"
        else:
            print("\n== 请用小红书 App 扫描上面那张二维码，并在手机上点「确认登录」==")
            data = probe_loop(user_id, timeout=args.timeout, save_as="_live_qr.png")
            ok = data.get("state") == "logged_in"

        lb.stop(user_id)
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n结果：", "✅ 通过" if ok else "❌ 未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
