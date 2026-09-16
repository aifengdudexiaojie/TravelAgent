"""小红书 MCP 实例管理：每用户一份工作目录 + 独立端口（多用户方案核心）
================================================================================

为什么要这样做
--------------
`xiaohongshu-mcp` 是一个本地可执行文件（Windows exe），它在**自己的工作目录**里读写
`cookies.json` 保存登录态，默认监听 `:18060`。两个人共用一个实例时：

    · 共用同一个小红书账号（分不清是谁搜的、限流也算在一起）；
    · 谁点一次登录就会把另一个人的 cookies.json 覆盖掉（互相顶下线）。

所以"让每个用户登录自己的小红书"必须做到**一人一实例**：独立工作目录（隔离 cookies）
+ 独立端口（隔离 HTTP 接口）。

目录与端口约定
--------------
    xiaohongshumcp/
    ├── xiaohongshu-mcp-windows-amd64.exe      主服务（-port / -headless）
    ├── xiaohongshu-login-windows-amd64.exe    登录工具（打开浏览器扫码）
    ├── cookies.json                           共享模式的登录态
    └── users/<user_id>/
        ├── cookies.json                       该用户自己的登录态
        └── instance.json                      {port, pid, started_at}

    共享模式（XHS_MULTI_USER=false）：所有用户都用默认实例（:18060，cookies 在 MCP 根目录）
    多用户模式（XHS_MULTI_USER=true，默认）：每个用户一个实例，端口从 XHS_PORT_BASE 递增

环境变量
--------
    XHS_MULTI_USER       true|false   是否一人一实例（默认 true）
    XHS_MCP_URL          默认实例地址（共享模式用，默认 http://localhost:18060/mcp）
    XHS_PORT_BASE        多用户模式端口起点（默认 18100）
    XHS_MAX_INSTANCES    同时保留的实例上限（默认 3，超出提示稍后再试）
    XHS_START_TIMEOUT    启动等待秒数（默认 45）
    XHS_IDLE_TIMEOUT     空闲多久回收实例（秒，默认 1800；0=不回收）
    XHS_HEADLESS         true|false   实例是否无头（默认 true；调试可见浏览器设 false）
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("services.xhs_manager")

ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = ROOT / "xiaohongshumcp"                 # ⚠️ 目录名没有连字符
USERS_DIR = MCP_DIR / "users"
LOG_DIR = ROOT / "logs"

# 官方 releases 的命名约定：xiaohongshu-mcp-<os>-<arch>[.exe]
# ⚠️ 不要在代码里写死 Windows 文件名：云端 Linux 服务器跑不了 PE 文件（会报
#    Permission denied / Exec format error），必须按当前平台挑对应构建。
_EXE_CANDIDATES = {
    "mcp": {
        "win32": ["xiaohongshu-mcp-windows-amd64.exe", "xiaohongshu-mcp.exe"],
        "linux": ["xiaohongshu-mcp-linux-amd64", "xiaohongshu-mcp-linux-arm64", "xiaohongshu-mcp"],
        "darwin": ["xiaohongshu-mcp-darwin-arm64", "xiaohongshu-mcp-darwin-amd64", "xiaohongshu-mcp"],
    },
    "login": {
        "win32": ["xiaohongshu-login-windows-amd64.exe", "xiaohongshu-login.exe"],
        "linux": ["xiaohongshu-login-linux-amd64", "xiaohongshu-login-linux-arm64", "xiaohongshu-login"],
        "darwin": ["xiaohongshu-login-darwin-arm64", "xiaohongshu-login-darwin-amd64", "xiaohongshu-login"],
    },
}
# 允许用环境变量指定可执行文件（自编译/自定义路径）
_EXE_ENV = {"mcp": "XHS_MCP_EXE", "login": "XHS_LOGIN_EXE"}
_DOWNLOAD_HINT = "https://github.com/xpzouying/xiaohongshu-mcp/releases"
COOKIE_MAX_BYTES = 2 * 1024 * 1024          # cookies.json 上限 2MB
COOKIE_MIN_BYTES = 500                      # 小于此值视为"占位文件"（MCP 启动会写 ~99B 占位）


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform.startswith("darwin"):
        return "darwin"
    return "linux"


PLATFORM = _platform_key()


def _is_windows_pe(path: Path) -> bool:
    """看文件头是不是 Windows PE（MZ）——用来给出"你放错平台了"的明确提示。"""
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


def resolve_exe(kind: str = "mcp") -> tuple[Optional[Path], str]:
    """按当前平台解析可执行文件。返回 (path, error_message)；成功时 error 为空串。

    会顺带做两件容易被忽略的事：
      · POSIX 下缺可执行位时自动 chmod +x（从 Windows 拷过去的文件通常没有 x 位）；
      · 如果找到的是 Windows PE 却在 Linux/macOS 上跑，直接给出"下载 Linux 版"的明确指引，
        而不是丢一个 "Permission denied" 让人猜。
    """
    assert kind in _EXE_CANDIDATES, kind

    explicit = os.getenv(_EXE_ENV[kind], "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            return None, f"{_EXE_ENV[kind]} 指向的文件不存在：{path}"
        ok, err = _check_runnable(path, kind)
        return (path, "") if ok else (None, err)

    names = _EXE_CANDIDATES[kind][PLATFORM]
    existing = [MCP_DIR / n for n in names if (MCP_DIR / n).is_file()]
    if not existing:
        # 常见误操作：把 Windows 版文件拷到 Linux 服务器上 —— 单独点出来
        other = [p for p in MCP_DIR.glob(f"xiaohongshu-{kind}-*") if p.is_file()]
        hint = ""
        if other and PLATFORM != "win32":
            names_in_dir = "、".join(p.name for p in other[:3])
            hint = (f"\n  ⚠️ 目录里检测到这些文件：{names_in_dir}\n"
                    f"     但它们不是 {PLATFORM} 平台的可执行文件（Windows 的 .exe 在 Linux 上无法运行）。")
        return None, (
            f"未找到 {kind} 可执行文件（当前平台：{PLATFORM}）。\n"
            f"  已查找：{MCP_DIR}/{{{', '.join(names)}}}{hint}\n"
            f"  请到 {_DOWNLOAD_HINT} 下载对应平台的文件并放到 {MCP_DIR}/"
        )

    for path in existing:
        ok, err = _check_runnable(path, kind)
        if ok:
            return path, ""
    # 找到文件但不可用：把原因带出去
    path = existing[0]
    _, err = _check_runnable(path, kind)
    return None, err


def _check_runnable(path: Path, kind: str) -> tuple[bool, str]:
    if PLATFORM == "win32":
        return True, ""
    if _is_windows_pe(path):
        return False, (
            f"{path.name} 是 **Windows** 可执行文件，当前服务器是 {PLATFORM}，无法运行。\n"
            f"  请在服务器上改用对应平台的构建，例如 linux-amd64：\n"
            f"    wget -P {MCP_DIR} {_DOWNLOAD_HINT.replace('/releases', '')}/releases/latest/download/xiaohongshu-{kind}-linux-amd64\n"
            f"    chmod +x {MCP_DIR}/xiaohongshu-{kind}-linux-amd64\n"
            f"  或改用「导入 cookies.json」的方式（见 docs/xhs-multi-user.md 第五节）"
        )
    # 缺可执行位 → 自动补上（从 Windows 复制过来的文件常见）
    if not os.access(path, os.X_OK):
        try:
            path.chmod(path.stat().st_mode | 0o755)
            logger.info("已为 %s 补上可执行权限", path)
        except OSError as exc:
            return False, f"{path.name} 没有可执行权限，且自动 chmod 失败：{exc}\n  请手动执行：chmod +x {path}"
    return True, ""


def _env_flag(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


MULTI_USER = _env_flag("XHS_MULTI_USER", True)
DEFAULT_URL = os.getenv("XHS_MCP_URL", "http://localhost:18060/mcp").strip()
PORT_BASE = int(os.getenv("XHS_PORT_BASE", "18100"))
MAX_INSTANCES = int(os.getenv("XHS_MAX_INSTANCES", "3"))
START_TIMEOUT = float(os.getenv("XHS_START_TIMEOUT", "180"))   # 首次运行要下载无头浏览器（~150MB）
IDLE_TIMEOUT = float(os.getenv("XHS_IDLE_TIMEOUT", "1800"))
HEADLESS = _env_flag("XHS_HEADLESS", True)


# ================================================================
# 实例
# ================================================================
@dataclass
class Instance:
    user_id: str
    port: int
    workdir: Path
    proc: Optional[subprocess.Popen] = None
    started_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/mcp"

    @property
    def cookie_file(self) -> Path:
        return self.workdir / "cookies.json"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def touch(self) -> None:
        self.last_used_at = time.time()


_instances: Dict[str, Instance] = {}
_ports_in_use: set[int] = set()
_lock = threading.RLock()


def multi_user_enabled() -> bool:
    return MULTI_USER


def _safe_name(user_id: str) -> str:
    """把 user_id 变成安全的目录名。

    先替换掉一切非 [A-Za-z0-9_.-] 的字符（路径分隔符因此不可能出现），
    再处理"整名都是点"的情况（`.`/`..` 会让 USERS_DIR/x 变成别的目录）。
    """
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(user_id or "anonymous"))[:64].strip("._-")
    if not name or set(name) <= {"."}:
        # 极端输入（空、全点号）→ 用哈希兜底，绝不产生 "." / ".." 这种目录名
        import hashlib
        name = "u" + hashlib.sha1(str(user_id).encode("utf-8")).hexdigest()[:16]
    return name


def workdir_for(user_id: str) -> Path:
    """多用户模式：一人一目录；共享模式：MCP 根目录。

    目录名经过 `_safe_name()` 清洗，保证结果**始终**在 USERS_DIR 之下（不会被 `..` 穿出去）。
    """
    if not MULTI_USER:
        return MCP_DIR
    path = USERS_DIR / _safe_name(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _allocate_port() -> int:
    port = PORT_BASE
    while port in _ports_in_use:
        port += 1
    _ports_in_use.add(port)
    return port


def _release_port(port: int) -> None:
    _ports_in_use.discard(port)


def instance_for(user_id: str, touch: bool = True) -> Optional[Instance]:
    """取该用户的实例。touch=False 用于"只查状态"——不要因为轮询就把空闲计时刷新掉。"""
    with _lock:
        inst = _instances.get(_safe_name(user_id))
        if inst is not None and touch:
            inst.touch()
        return inst


def mcp_url_for(user_id: Optional[str], auto_start: bool = True) -> str:
    """分析流程要用的 MCP 地址。

    · 共享模式：始终用默认实例
    · 多用户模式：用该用户自己的实例；auto_start=True 时若没起就先起一个

    ⚠️ 只查状态时（前端每 5 秒轮询）必须传 auto_start=False：否则每个打开攻略页的
    用户都会被拉起一个 Chromium 实例，几个访客就把实例上限占满。
    """
    if not MULTI_USER or not user_id:
        return DEFAULT_URL
    inst = instance_for(str(user_id), touch=auto_start)
    if inst is not None and inst.alive():
        return inst.url
    if not auto_start:
        return DEFAULT_URL
    result = start_mcp(str(user_id))
    return result.get("url") or DEFAULT_URL


# ================================================================
# 启停
# ================================================================
def _mcp_exe_path() -> Optional[Path]:
    """当前平台下可用的 MCP 可执行文件（没有则 None）。"""
    return resolve_exe("mcp")[0]


def _exe_available() -> tuple[bool, str]:
    """MCP 主程序是否可用（按当前平台解析；错误信息可直接展示给用户）。"""
    path, err = resolve_exe("mcp")
    return (path is not None), err


def _http_reachable(url: str, timeout: float = 2.0) -> bool:
    import httpx
    try:
        resp = httpx.get(url, timeout=timeout)
        return resp.status_code < 500
    except Exception:
        return False


def start_mcp(user_id: str, wait: bool = True) -> Dict[str, Any]:
    """启动（或复用）用户自己的 MCP 实例。幂等。

    wait=False：只把进程拉起来就返回（{"pending": True}），不在这里等它就绪 ——
    首次运行 MCP 会下载约 150MB 的无头浏览器，阻塞式等待很容易把 HTTP 请求拖超时
    （前端表现为"一直在获取二维码，最后显示不可用"）。调用方改为轮询即可。
    """
    mcp_exe, err = resolve_exe("mcp")
    if mcp_exe is None:
        logger.error("小红书 MCP 不可用：%s", err)
        return {"ok": False, "url": DEFAULT_URL, "message": err}

    if not MULTI_USER:
        # 共享模式：默认实例由运维启动，这里只探活
        alive = _http_reachable(DEFAULT_URL)
        return {
            "ok": alive, "url": DEFAULT_URL, "shared": True,
            "message": "共享实例已就绪" if alive else "共享实例未启动，请先运行 python start_mcp.py",
        }

    with _lock:
        inst = _instances.get(_safe_name(user_id))
        if inst is not None and inst.alive():
            inst.touch()
            return {"ok": True, "url": inst.url, "port": inst.port, "message": "实例已在运行",
                    "reused": True}

        if len(_instances) >= MAX_INSTANCES:
            live = [i for i in _instances.values() if i.alive()]
            if len(live) >= MAX_INSTANCES:
                return {"ok": False, "url": DEFAULT_URL,
                        "message": f"当前已有 {len(live)} 个小红书实例在跑（上限 {MAX_INSTANCES}），请稍后再试"}

        port = _allocate_port()
        workdir = workdir_for(user_id)
        log_path = LOG_DIR / f"xhs-mcp-{_safe_name(user_id)}.log"
        LOG_DIR.mkdir(exist_ok=True)

        cmd = [str(mcp_exe), f"-port=:{port}", f"-headless={'true' if HEADLESS else 'false'}"]
        logger.info("启动小红书 MCP：user=%s port=%d cwd=%s", user_id, port, workdir)
        try:
            with open(log_path, "a", encoding="utf-8") as log_fp:
                log_fp.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start {' '.join(cmd)} =====\n")
                log_fp.flush()
                proc = subprocess.Popen(cmd, cwd=str(workdir), stdout=log_fp, stderr=log_fp)
        except Exception as exc:
            _release_port(port)
            logger.exception("启动 MCP 失败: %s", exc)
            return {"ok": False, "url": DEFAULT_URL, "message": f"启动失败：{exc}"}

        inst = Instance(user_id=_safe_name(user_id), port=port, workdir=workdir, proc=proc)
        _instances[inst.user_id] = inst
        (workdir / "instance.json").write_text(
            json.dumps({"port": port, "pid": proc.pid, "started_at": inst.started_at},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    if not wait:
        return {"ok": False, "pending": True, "url": inst.url, "port": inst.port, "pid": proc.pid,
                "message": "小红书实例正在启动…（首次运行需下载无头浏览器，约 150MB，请稍候）"}

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if not inst.alive():
            _release_port(inst.port)
            with _lock:
                _instances.pop(inst.user_id, None)
            return {"ok": False, "url": DEFAULT_URL, "log_tail": instance_log_tail(user_id),
                    "message": f"MCP 进程启动后立即退出，日志尾部：\n{instance_log_tail(user_id)}"}
        if _http_reachable(inst.url):
            return {"ok": True, "url": inst.url, "port": inst.port, "pid": proc.pid,
                    "message": "MCP 实例已启动"}
        time.sleep(0.6)

    return {"ok": False, "url": inst.url, "port": inst.port, "log_tail": instance_log_tail(user_id),
            "message": (f"MCP 启动超时（{START_TIMEOUT:.0f}s）。日志尾部：\n{instance_log_tail(user_id)}\n"
                        f"提示：首次运行需下载无头浏览器（约 150MB），网络受限时会较慢；"
                        f"可用 XHS_START_TIMEOUT 调大等待时间")}


def instance_ready(user_id: str) -> bool:
    """实例进程活着且 HTTP 已经能连通（可以取二维码/检索了）。"""
    inst = instance_for(user_id, touch=False)
    if inst is None or not inst.alive():
        return False
    return _http_reachable(inst.url)


def instance_log_tail(user_id: str, lines: int = 8) -> str:
    """取该用户 MCP 实例日志的尾部（排查启动失败用）。"""
    path = LOG_DIR / f"xhs-mcp-{_safe_name(user_id)}.log"
    try:
        if not path.exists():
            return ""
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            data = fh.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""


def stop_mcp(user_id: str) -> bool:
    with _lock:
        inst = _instances.pop(_safe_name(user_id), None)
    if inst is None:
        return False
    if inst.proc is not None and inst.proc.poll() is None:
        try:
            inst.proc.terminate()
            inst.proc.wait(timeout=8)
        except Exception:
            try:
                inst.proc.kill()
            except Exception:
                pass
    _release_port(inst.port)
    logger.info("已停止小红书 MCP：user=%s port=%d", user_id, inst.port)
    return True


def stop_all() -> None:
    for user_id in list(_instances):
        stop_mcp(user_id)


def reap_idle() -> int:
    """回收空闲超时的实例（后台线程周期调用）。"""
    if IDLE_TIMEOUT <= 0:
        return 0
    now = time.time()
    killed = 0
    for user_id, inst in list(_instances.items()):
        if now - inst.last_used_at > IDLE_TIMEOUT:
            stop_mcp(user_id)
            killed += 1
    return killed


# ================================================================
# 登录
# ================================================================
def start_login(user_id: str) -> Dict[str, Any]:
    """拉起登录工具（会用**该用户自己的工作目录**，因此写的是他自己的 cookies.json）。

    登录工具是 GUI 程序：会弹出浏览器窗口，用户扫码完成后窗口自动关闭并落盘 cookies。
    """
    login_exe, err = resolve_exe("login")
    if login_exe is None:
        logger.error("小红书登录程序不可用：%s", err)
        return {"ok": False, "message": err}

    workdir = workdir_for(user_id)
    log_path = LOG_DIR / f"xhs-login-{_safe_name(user_id)}.log"
    LOG_DIR.mkdir(exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as log_fp:
            log_fp.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} login =====\n")
            log_fp.flush()
            subprocess.Popen([str(login_exe)], cwd=str(workdir), stdout=log_fp, stderr=log_fp)
    except Exception as exc:
        logger.exception("拉起登录程序失败: %s", exc)
        return {"ok": False, "message": f"拉起登录程序失败：{exc}"}

    logger.info("已拉起小红书登录程序：user=%s cwd=%s", user_id, workdir)
    return {
        "ok": True,
        "message": "已打开小红书登录窗口，请扫码登录；登录完成后状态会自动变绿",
        "workdir": str(workdir),
        "cookie_file": str(workdir / "cookies.json"),
    }


def login_qrcode(user_id: str) -> Dict[str, Any]:
    """取登录二维码（**云上用户登录的正解**，无需桌面/弹窗，也不用碰 cookies 文件）。

    流程：确保该用户实例在跑 → 调 MCP 的 `get_login_qrcode` 工具 → 返回 Base64 图片，
    前端直接 <img src="data:image/png;base64,..."> 展示，用户用小红书 App 扫码即可。

    返回 {"ok", "image_base64", "mime", "text", "expires_at", "mcp_url", "message"}
    """
    from xiaohongshu_mcp_client import get_login_qrcode as _qr

    inst = instance_for(user_id, touch=True)
    url = inst.url if inst is not None else DEFAULT_URL          # ⚠️ 必须在分支外先赋值
    reachable = inst is not None and inst.alive() and _http_reachable(url)

    if not reachable:
        # 实例没起 / 还没就绪 → 后台拉起，接口**立刻**返回 pending，由前端轮询。
        # （不能在这里阻塞等待：首次运行要下载 ~150MB 浏览器，会把 HTTP 请求拖超时）
        started = start_mcp(user_id, wait=not MULTI_USER)
        url = started.get("url") or url
        if not _http_reachable(url):
            waited = int(time.time() - inst.started_at) if inst is not None else 0
            if started.get("pending") or started.get("ok") or started.get("reused"):
                return {
                    "ok": False, "pending": True, "mcp_url": url, "waited": waited,
                    "message": (started.get("message") or "小红书实例正在启动…")
                               + (f"（已等待 {waited}s）" if waited > 5 else ""),
                }
            return {"ok": False, "mcp_url": url,
                    "message": started.get("message") or "小红书实例不可用",
                    "log_tail": started.get("log_tail", "")}

    try:
        qr = _qr(url)
    except Exception as exc:
        logger.warning("获取登录二维码失败 user=%s: %s", user_id, exc)
        tail = instance_log_tail(user_id)
        return {"ok": False, "mcp_url": url, "log_tail": tail,
                "message": f"获取二维码失败：{exc}" + (f"\n实例日志尾部：\n{tail}" if tail else "")}

    if not qr.get("image_base64"):
        return {
            "ok": False, "mcp_url": url, "text": qr.get("text", ""),
            "message": qr.get("text") or "未取到二维码（可能已登录；如需换号请先退出登录）",
        }
    return {
        "ok": True,
        "mcp_url": url,
        "mime": qr.get("mime", "image/png"),
        "image_base64": qr["image_base64"],
        "text": qr.get("text", ""),
        "expires_at": qr.get("expires_at"),
        "message": qr.get("text") or "请用小红书 App 扫码登录",
    }


def clear_login(user_id: str) -> Dict[str, Any]:
    """退出登录：停实例 → 让 MCP 删除 cookies → 清掉本地 cookies.json（用于换号/重扫）。"""
    from xiaohongshu_mcp_client import _batch_call

    inst = instance_for(user_id, touch=False)
    if inst is not None and inst.alive():
        try:
            _batch_call("tools/call", {"name": "delete_cookies"},
                        base_url=inst.url, max_retries=0)
        except Exception as exc:
            logger.info("delete_cookies 调用失败（继续清理本地文件）: %s", exc)
    stop_mcp(user_id)

    path = workdir_for(user_id) / "cookies.json"
    removed = False
    try:
        if path.exists():
            path.unlink()
            removed = True
    except OSError as exc:
        return {"ok": False, "message": f"删除 cookies.json 失败：{exc}"}
    logger.info("用户 %s 已退出登录（删除 cookies=%s）", user_id, removed)
    return {"ok": True, "removed": removed,
            "message": "已退出登录，可重新扫码登录（或导入其他账号的 cookies.json）"}


def cookie_present(user_id: str) -> bool:
    """该用户目录里是否有**真实**的登录态。

    ⚠️ 注意：MCP 启动时会自己写一个约 99 字节的占位 cookies.json，
    所以不能用"文件存在"或很小的阈值判断 —— 真实登录态通常有几 KB。
    这里用 500 字节作为分界（占位 99B < 500 < 真实 ~7KB）。
    """
    path = workdir_for(user_id) / "cookies.json"
    try:
        return path.exists() and path.stat().st_size >= COOKIE_MIN_BYTES
    except OSError:
        return False


def import_cookies(user_id: str, raw: Any) -> Dict[str, Any]:
    """导入 cookies.json —— 无桌面服务器上替代"扫码登录"的落地方式。

    为什么需要它：`xiaohongshu-login-*` 是**桌面程序**（要弹浏览器扫码），
    云服务器（尤其没有 X/桌面的 Linux）根本弹不出来。所以支持把在**本机**
    登录好的 `cookies.json` 传上来，直接放进该用户自己的工作目录。

    校验：非空、≤2MB、合法 JSON（对象或数组）。
    """
    data = raw.encode("utf-8") if isinstance(raw, str) else (raw or b"")
    # 校验顺序：空 → 过大 → 能否解析 → 内容像不像 cookies.json → 是否过短
    # （先解析再判长度，这样 "{}" 这类会得到"内容不像"这种更有用的提示）
    if not data:
        return {"ok": False, "message": "内容为空，请上传完整的 cookies.json"}
    if len(data) > COOKIE_MAX_BYTES:
        return {"ok": False, "message": f"文件过大（上限 {COOKIE_MAX_BYTES // 1024 // 1024} MB）"}
    try:
        parsed = json.loads(data.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"不是合法的 JSON：{exc}"}
    if not isinstance(parsed, (dict, list)) or not parsed:
        return {"ok": False, "message": "内容不像 cookies.json（应为非空的 JSON 对象或数组）"}
    if len(data) < 10:
        return {"ok": False, "message": "内容过短，请上传完整的 cookies.json"}

    workdir = workdir_for(user_id)
    path = workdir / "cookies.json"
    try:
        path.write_bytes(data)
        if os.name != "nt":
            path.chmod(0o600)             # 凭据文件，权限收紧
    except OSError as exc:
        return {"ok": False, "message": f"写入失败：{exc}"}

    # 实例正在跑时重启一次，让新的 cookies 生效
    restarted = False
    inst = instance_for(user_id, touch=False)
    if inst is not None and inst.alive():
        stop_mcp(user_id)
        started = start_mcp(user_id)
        restarted = bool(started.get("ok"))
    logger.info("用户 %s 导入了 cookies.json（%d 字节，重启实例=%s）", user_id, len(data), restarted)
    return {
        "ok": True,
        "bytes": len(data),
        "path": str(path),
        "restarted": restarted,
        "message": "cookies.json 已导入" + ("，实例已重启以生效" if restarted else "；启动实例后即可使用"),
    }


def login_status(user_id: str) -> Dict[str, Any]:
    """查询该用户的小红书登录状态。

    **只读**：绝不会因为"查状态"而启动实例（前端轮询很频繁，起实例代价很大）。
    实例没起时直接返回 mcp_running=False，由前端引导用户点「登录小红书」。
    """
    from xiaohongshu_mcp_client import _batch_call

    inst = instance_for(user_id, touch=False) if MULTI_USER else None
    running = bool(inst is not None and inst.alive())
    url = inst.url if running else DEFAULT_URL
    info: Dict[str, Any] = {
        "logged_in": False,
        "shared": not MULTI_USER,
        "mcp_url": url,
        "mcp_running": running,
        "has_cookie": cookie_present(user_id),
        "port": inst.port if running else None,
        "message": "",
        "started": running,
    }

    if MULTI_USER and not running:
        # 服务器上没有可用的 MCP 程序时，直接把原因说清楚（而不是让用户猜）
        exe_path, exe_err = resolve_exe("mcp")
        if exe_path is None:
            info["message"] = f"服务器上无法使用小红书服务：{exe_err}"
            info["exe_error"] = exe_err
        elif not info["has_cookie"]:
            info["message"] = "尚未启动小红书实例：点「登录小红书」会自动启动并弹出扫码窗口"
        else:
            info["message"] = "检测到已保存的登录态，点「登录小红书」即可启动实例继续使用"
        return info

    if inst is not None and not inst.alive():
        info["message"] = "MCP 实例进程已退出，请点「登录小红书」重新启动"
        return info

    try:
        result = _batch_call("tools/call", {"name": "check_login_status"}, base_url=url,
                             max_retries=0)
    except ConnectionError:
        info["message"] = ("MCP 实例未启动" if MULTI_USER else "共享 MCP 服务未启动，请先运行 python start_mcp.py")
        return info
    except Exception as exc:
        info["message"] = f"状态检查失败：{exc}"
        return info

    info["mcp_running"] = True
    text = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
    # MCP 返回文本里含"已登录"即视为登录成功（该工具的成功文案固定为 ✅ 已登录 / 用户名: xxx）
    info["logged_in"] = "已登录" in text
    info["raw"] = text[:300]
    if info["logged_in"]:
        match = re.search(r"用户名[:：]\s*(\S+)", text)
        info["username"] = match.group(1) if match else ""
        info["message"] = f"已登录{('：' + info['username']) if info.get('username') else ''}"
    else:
        info["message"] = "未登录，请点击「登录小红书」扫码"
    return info


def status_for(user_id: str) -> Dict[str, Any]:
    """给前端的总状态（含模式信息与上限）。只读，不启动实例。"""
    inst = instance_for(user_id, touch=False) if MULTI_USER else None
    data = login_status(user_id)
    data.update({
        "multi_user": MULTI_USER,
        "max_instances": MAX_INSTANCES,
        "running_instances": len([i for i in _instances.values() if i.alive()]),
        "starting": bool(inst is not None and inst.alive() and not data.get("mcp_running")),
        "platform": PLATFORM,
        "exe_available": _mcp_exe_path() is not None,
        "login_exe_available": resolve_exe("login")[0] is not None,
        "exe_error": resolve_exe("mcp")[1],
        "login_exe_error": resolve_exe("login")[1],
        "workdir": str(workdir_for(user_id)),
    })
    if inst is not None:
        data["started_at"] = inst.started_at
    return data
