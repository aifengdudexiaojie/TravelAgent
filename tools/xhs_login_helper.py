"""小红书登录助手（在**用户自己的电脑**上运行）
================================================================================

为什么需要它
------------
服务器直接登录小红书时，XHS 看到的是"云主机 IP + 全新设备"，容易触发风控
（要短信验证码、甚至警告账号）。而在用户自己的电脑上登录就是一次完全正常的登录。
这个助手的做法：

  ① 在你电脑上弹出一个浏览器，打开小红书登录页（你正常扫码/手机号登录）
  ② 你登录成功后，助手把你浏览器里的完整登录态取出来
    （***** 关键：注册凭据 web_session 是 HttpOnly，网页 JS 读不到，
    只有"自己控制浏览器"的程序才能拿到 —— 这就是为什么需要助手/扩展 *****）
  ③ 用网页上生成的配对码把登录态交给服务器，服务器写进你的账号目录

用法（用户侧）
--------------
  xhs-login-helper.exe --server http://你的站点 --code ABCD2345

或者不带参数运行，按提示输入。

依赖：Python 3.9+，仅需 websockets（打包成 EXE 后无需任何依赖）
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

LOGIN_URL = "https://www.xiaohongshu.com/explore"
LOGIN_SEL = ".main-container .user .link-wrapper .channel"   # 出现即已登录

CHROME_CANDIDATES = {
  "win32": [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\xiaohongshu-mcp\browser\148.0.7778.215\browser\chrome.exe",
  ],
  "darwin": [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ],
  "linux": [
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
  ],
}


def out(msg: str = "") -> None:
  """输出：中文 Windows 控制台默认是 GBK，emoji 会直接抛 UnicodeEncodeError。

  所以这里三层保险：① 在 main 里 reconfigure(errors="replace")；
  ② 文案里不使用 emoji（全部用 [OK]/[X]/-> 这类纯字符）；
  ③ 兜底再退化成 ASCII 输出 —— 宁可难看，也绝不因为打印一句话把助手搞崩。
  """
  try:
    print(msg, flush=True)
  except Exception:
    try:
      sys.stdout.write(msg.encode("ascii", "replace").decode("ascii") + "\n")
      sys.stdout.flush()
    except Exception:
      pass


def find_browser() -> str:
  key = "win32" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
  for cand in CHROME_CANDIDATES.get(key, []):
    path = os.path.expandvars(cand)
    if os.path.exists(path):
      return path
  raise SystemExit("[X] 没找到 Chrome/Edge/Chromium，请先安装任意一个浏览器再运行本助手")


def launch_browser(profile_dir: Path) -> subprocess.Popen:
  """用独立 profile 启动浏览器，并打开远程调试端口。

  port=0 让系统随机分配，端口号会写到 profile 目录下的 DevToolsActivePort 文件里
  （比自己挑端口稳，不会撞车）。
  """
  browser = find_browser()
  try:
    (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)  # 见 wait_devtools_port 的说明
  except OSError:
    pass
  cmd = [
    browser,
    "--remote-debugging-port=0",
    f"--user-data-dir={profile_dir}",
    "--no-first-run", "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--lang=zh-CN", "--window-size=1200,860",
    LOGIN_URL,
  ]
  out(f"- 浏览器：{browser}")
  return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_devtools_port(profile_dir: Path, proc: subprocess.Popen,
            timeout: float = 40.0) -> int:
  """等 Chrome 写出调试端口。

  [!] 调用前会删掉旧的 DevToolsActivePort：Chrome 是单实例的，若上一次的助手浏览器
  还开着，新进程会把请求交给它然后**自己退出**，此时旧文件里的端口会误导我们
  （表现为"连不上调试端口"）。删掉旧文件 + 盯着进程是否还活着，才能给出准确提示。
  """
  port_file = profile_dir / "DevToolsActivePort"
  deadline = time.time() + timeout
  while time.time() < deadline:
    if port_file.exists():
      try:
        first = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
        if first.isdigit():
          return int(first)
      except Exception:
        pass
    if proc.poll() is not None:
      raise SystemExit(
        "[X] 浏览器启动后立即退出了 —— 多半是上一次的助手浏览器还开着。\n"
        "  请先关掉那个浏览器窗口，再重新运行本助手。")
    time.sleep(0.3)
  raise SystemExit("[X] 浏览器没有正常启动（拿不到调试端口）。请确认浏览器能手动打开。")


def page_target(port: int, timeout: float = 30.0) -> dict:
  deadline = time.time() + timeout
  last = None
  while time.time() < deadline:
    try:
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
        targets = json.loads(r.read().decode("utf-8", "ignore"))
      pages = [t for t in targets if t.get("type") == "page"
           and t.get("webSocketDebuggerUrl")]
      for t in pages:
        if "xiaohongshu.com" in (t.get("url") or ""):
          return t
      if pages:
        last = pages[0]
    except Exception:
      pass
    time.sleep(0.5)
  if last:
    return last
  raise SystemExit("[X] 没能连上浏览器页面，请重试")


class CDP:
  """极简 CDP 客户端（只用 websockets，避免把 playwright 打进包里）。

  每次调用开一条连接（用 with 语法，兼容 websockets 13+ 的要求）：
  我们只做"读 cookie / 判断是否登录"这种低频调用，连接开销可以忽略，
  换来的是不依赖 websockets 的私有持连行为。
  """

  def __init__(self, ws_url: str):
    self._url = ws_url

  def call(self, method: str, params: dict | None = None, timeout: float = 20.0):
    import websockets.sync.client as ws_client # type: ignore
    with ws_client.connect(self._url, max_size=None, open_timeout=15) as ws:
      ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
      deadline = time.time() + timeout
      while time.time() < deadline:
        raw = ws.recv(timeout=max(1.0, deadline - time.time()))
        try:
          msg = json.loads(raw)
        except Exception:
          continue
        if msg.get("id") == 1:
          if "error" in msg:
            raise RuntimeError(f"{method} 失败：{msg['error']}")
          return msg.get("result", {})
    raise TimeoutError(f"{method} 超时")

  def close(self) -> None:     # 无需关闭（无持连）
    return None


def is_logged_in(cdp: CDP) -> bool:
  try:
    res = cdp.call("Runtime.evaluate",
            {"expression": f"!!document.querySelector('{LOGIN_SEL}')",
            "returnByValue": True}, timeout=10)
    return bool(res.get("result", {}).get("value"))
  except Exception:
    return False


def all_cookies(cdp: CDP) -> list:
  res = cdp.call("Network.getAllCookies", {}, timeout=20)
  cookies = res.get("cookies") or []
  keep = []
  for c in cookies:
    name = c.get("name")
    if not name:
      continue
    item = {
      "name": name,
      "value": c.get("value") or "",
      "domain": c.get("domain") or ".xiaohongshu.com",
      "path": c.get("path") or "/",
      "httpOnly": bool(c.get("httpOnly")),
      "secure": bool(c.get("secure")),
      "sameSite": c.get("sameSite") if c.get("sameSite") in ("Strict", "Lax", "None") else "Lax",
    }
    exp = c.get("expires")
    if isinstance(exp, (int, float)) and exp > 0:
      item["expires"] = float(exp)
    keep.append(item)
  return keep


def upload(server: str, code: str, cookies: list) -> dict:
  body = json.dumps({"code": code, "cookies": json.dumps(cookies, ensure_ascii=False)}).encode()
  req = urllib.request.Request(
    server.rstrip("/") + "/api/xhs/pair/upload", data=body,
    headers={"Content-Type": "application/json"}, method="POST")
  with urllib.request.urlopen(req, timeout=60) as resp:
    return json.loads(resp.read().decode("utf-8", "ignore"))


def main() -> int:
  # 中文 Windows 控制台是 GBK：编不出的字符替换掉，别让打印把程序搞崩
  try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
  except Exception:
    pass

  ap = argparse.ArgumentParser(description="小红书登录助手：本机登录后把登录态同步给服务器")
  ap.add_argument("--server", help="你的站点地址，例如 http://124.222.174.240")
  ap.add_argument("--code", help="网页上生成的配对码")
  ap.add_argument("--timeout", type=float, default=300, help="等待登录的秒数（默认 5 分钟）")
  ap.add_argument("--print-cookies", action="store_true", help="调试用：打印 cookie 名字（不打印值）")
  args = ap.parse_args()

  out("=" * 68)
  out(" 小红书登录助手")
  out("=" * 68)
  server = args.server or input("服务器地址（例如 http://124.222.174.240）：").strip()
  code = (args.code or input("网页上生成的配对码：").strip()).upper()
  if not server or not code:
    out("[X] 服务器地址和配对码都必须提供")
    return 1

  profile_dir = Path(tempfile.gettempdir()) / "xhs-login-helper-profile"
  profile_dir.mkdir(parents=True, exist_ok=True)
  out(f"- 使用独立浏览器配置：{profile_dir}（下次运行会复用，设备身份稳定）")
  proc = launch_browser(profile_dir)
  try:
    port = wait_devtools_port(profile_dir, proc)
    out(f"- 调试端口：{port}")
    target = page_target(port)
    cdp = CDP(target["webSocketDebuggerUrl"])
    cdp.call("Network.enable")
    cdp.call("Runtime.enable")

    cookies = all_cookies(cdp)
    def session_value(items: list) -> str:
      return next((c["value"] for c in items if c["name"] == "web_session"), "")

    web_session_before = session_value(cookies)
    out("")
    out("-> 请在弹出的浏览器窗口里登录小红书（扫码或手机号登录都可以）。")
    out("  登录完成后本助手会自动把登录态同步到服务器，无需其他操作。")
    out("")

    deadline = time.time() + args.timeout
    logged = False
    how = ""
    while time.time() < deadline:
      if proc.poll() is not None:
        out("[X] 浏览器被关闭了，同步中止。请重新运行助手，并在登录完成前不要关窗口。")
        return 1
      if is_logged_in(cdp):
        logged, how = True, "页面已显示登录状态"
        break
      # 第二路信号：注册凭据 web_session 被换成新值（有些页面 DOM 更新滞后）
      current = session_value(all_cookies(cdp))
      if current and web_session_before and current != web_session_before:
        logged, how = True, "登录凭据 cookie 已更新"
        break
      if current and not web_session_before:
        web_session_before = current     # 首次出现只当基准，避免游客会话误判
      time.sleep(2)

    if not logged:
      out("[超时] 等待超时（未检测到登录）。请重新运行助手重试。")
      return 1

    cookies = all_cookies(cdp)
    has_session = any(c["name"] == "web_session" and c["value"] for c in cookies)
    out(f"[OK] 检测到登录成功（{how}），共取到 {len(cookies)} 个 cookie"
      f"（web_session={'有' if has_session else '没有'}）")
    if args.print_cookies:
      out("  cookie 名字：" + ", ".join(sorted({c["name"] for c in cookies})))
    if not has_session:
      out("[X] 没取到 web_session，无法同步。请确认登录确实成功（页面上应显示你的昵称）。")
      return 1
    if web_session_before and web_session_before == next(
        (c["value"] for c in cookies if c["name"] == "web_session"), ""):
      out("[!] web_session 与登录前相同，可能这个浏览器里本来就是登录状态（也可以用）。")

    out("- 正在同步到服务器…")
    try:
      result = upload(server, code, cookies)
    except Exception as exc:
      out(f"[X] 同步失败（网络或地址问题）：{exc}")
      out("  请确认服务器地址可访问、配对码没有过期（10 分钟）。")
      return 1

    if result.get("ok"):
      out("")
      out("[OK] 同步成功！回到网页刷新一下，登录状态会变成绿色，可以生成攻略了。")
      out(f"  服务器返回：{result.get('message')}")
      return 0
    out(f"[X] 服务器拒绝了这次同步：{result.get('message')}")
    out("  （配对码是一次性的，且 10 分钟过期 —— 若已过期请在网页上重新生成）")
    return 1
  finally:
    try:
      proc.terminate()
    except Exception:
      pass


if __name__ == "__main__":
  try:
    code_exit = main()
  except KeyboardInterrupt:
    out("\n已取消。")
    code_exit = 1
  if os.name == "nt" and sys.stdout.isatty():
    input("\n按回车关闭…")
  raise SystemExit(code_exit)
