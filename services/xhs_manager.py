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
# 查登录状态是"每 2.5 秒轮询一次"的调用：MCP 忙起来可能长时间不返回，
# 用默认 120s 超时会把 /api/xhs/status 一起拖死（前端表现：扫码后页面毫无反应）。
STATUS_TIMEOUT = float(os.getenv("XHS_STATUS_TIMEOUT", "8"))
# ⚠️ 每次 check_login_status 都会让 MCP **新起一个 Chromium**（上游 service.go:
#    CheckLoginStatus → newBrowser() → 导航 /explore → 关掉），实测每次约 4 秒。
#    所以状态探测必须缓存/节流：不加节流 = 每 4 秒启一个浏览器，云主机直接被拖垮，
#    而且会跟"等扫码"的登录会话抢资源。
STATUS_CACHE_SECONDS = float(os.getenv("XHS_STATUS_CACHE", "30"))
# 等扫码窗口：MCP 侧 get_login_qrcode 会留一个浏览器等 4 分钟（源码里的 timeout）。
# 这段时间内**完全不能**去调 MCP 查状态，否则就是拿新浏览器去挤登录会话。
SCAN_WINDOW = float(os.getenv("XHS_SCAN_WINDOW", "300"))
# 二维码缓存时长：**绝不能**每次请求都去调 MCP 的 get_login_qrcode ——
# 上游 issue #799 明确：重复调用会新建浏览器并取消旧会话，
# 于是"用户刚在手机上确认的登录/设备验证上下文"会被顶掉，表现为扫码后永远没反应。
QR_CACHE_SECONDS = float(os.getenv("XHS_QR_CACHE", "120"))


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
# 二维码缓存：{user_key: {"at": ts, "payload": {...}}}（进程内，单副本部署假设不变）
_qr_cache: Dict[str, Dict[str, Any]] = {}
# 登录状态探测缓存：{user_key: {"at": ts, "payload": {...}}}
# （每次探测 = 一个 Chromium，必须节流）
_status_cache: Dict[str, Dict[str, Any]] = {}
# 扫码等待登记：{user_key: {"issued_at": ts, "cookie_at": mtime}}
# 有登记 = 用户正在扫码 → 期间**不碰 MCP**，只看 cookies.json 与 MCP 日志
_scan_watch: Dict[str, Dict[str, Any]] = {}


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


# ================================================================
# 浏览器运行时依赖诊断
# ----------------------------------------------------------------
# MCP 会自己下载一份 Chromium 到缓存目录（~/.cache/xiaohongshu-mcp/browser/<版本>/browser/chrome），
# 但它**不会**安装 Chromium 依赖的系统库；最小化的云服务器镜像默认没有这些库，于是启动时报：
#     chrome: error while loading shared libraries: libatk-1.0.so.0: cannot open shared object file
# 报错原文对用户毫无意义，所以这里把它翻译成"缺哪些库 + 复制粘贴就能修的命令"。
# ================================================================
_DEPS_SCRIPT = "deploy/install-xhs-deps.sh"

# .so 名 → Debian/Ubuntu 包名（只列最容易缺的，其余交给 apt-file）
_LIB_APT = {
    "libatk-1.0.so.0": "libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0",
    "libatspi.so.0": "libatspi2.0-0",
    "libcups.so.2": "libcups2",
    "libdrm.so.2": "libdrm2",
    "libgbm.so.1": "libgbm1",
    "libnss3.so": "libnss3",
    "libnssutil3.so": "libnss3",
    "libsmime3.so": "libnss3",
    "libnspr4.so": "libnspr4",
    "libplc4.so": "libnspr4",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libxcomposite.so.1": "libxcomposite1",
    "libxdamage.so.1": "libxdamage1",
    "libxfixes.so.3": "libxfixes3",
    "libxrandr.so.2": "libxrandr2",
    "libxext.so.6": "libxext6",
    "libxi.so.6": "libxi6",
    "libxtst.so.6": "libxtst6",
    "libx11.so.6": "libx11-6",
    "libx11-xcb.so.1": "libx11-xcb1",
    "libxcb.so.1": "libxcb1",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libpangocairo-1.0.so.0": "libpango-1.0-0",
    "libcairo.so.2": "libcairo2",
    "libglib-2.0.so.0": "libglib2.0-0",
    "libgobject-2.0.so.0": "libglib2.0-0",
    "libexpat.so.1": "libexpat1",
    "libfontconfig.so.1": "libfontconfig1",
    "libfreetype.so.6": "libfreetype6",
    "libdbus-1.so.3": "libdbus-1-3",
    "libasound.so.2": "libasound2",
}

_MISSING_LIB_RE = re.compile(r"error while loading shared libraries:\s*([^\s:]+)")
_DEPS_CACHE: Dict[str, Any] = {"libs": None, "at": 0.0, "key": ""}


def detect_missing_lib(text: str) -> str:
    """从日志/报错文本里抠出缺失的 .so 名（没有则返回空串）。"""
    if not text:
        return ""
    match = _MISSING_LIB_RE.search(text)
    return match.group(1) if match else ""


def browser_binary_path(user_id: Optional[str] = None) -> Optional[Path]:
    """定位 MCP 自己下载的 Chromium。

    优先取实例日志里 "using browser binary: <path>" 那句（最准），
    取不到再在缓存目录里按平台约定找。
    """
    if user_id:
        for line in reversed(instance_log_tail(user_id, lines=300).splitlines()):
            match = re.search(r"using browser binary:\s*(.+?)\s*$", line)
            if not match:
                continue
            candidate = Path(match.group(1).strip().strip('"'))
            if candidate.exists():
                return candidate

    roots = []
    if PLATFORM == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "xiaohongshu-mcp" / "browser")
    else:
        roots.append(Path.home() / ".cache" / "xiaohongshu-mcp" / "browser")
    for root in roots:
        if not root.exists():
            continue
        for name in ("chrome", "chrome.exe", "headless_shell", "chromium"):
            hits = sorted(root.glob(f"*/browser/{name}"))
            if hits:
                return hits[-1]
    return None


def browser_missing_libs(user_id: Optional[str] = None, max_age: float = 60.0) -> list:
    """用 ldd 查出 Chromium 还缺哪些系统库（非 Linux 或查不到时返回空列表）。

    结果缓存 max_age 秒：前端每 2.5s 轮询一次状态，不能每次都 fork 一个 ldd。
    """
    if PLATFORM != "linux":
        return []
    key = _safe_name(user_id) if user_id else ""
    now = time.time()
    cached = _DEPS_CACHE.get("libs")
    if cached is not None and _DEPS_CACHE.get("key") == key and now - float(_DEPS_CACHE.get("at") or 0) < max_age:
        return list(cached)

    libs: list = []
    binary = browser_binary_path(user_id)
    if binary is not None:
        try:
            proc = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, timeout=20)
            libs = sorted({line.split("=>")[0].strip()
                           for line in (proc.stdout or "").splitlines() if "not found" in line})
        except Exception:
            libs = []
    if not libs and user_id:
        # 浏览器还没下载 / ldd 跑不起来 → 退而求其次，从实例日志里抠库名
        lib = detect_missing_lib(instance_log_tail(user_id, lines=300))
        if lib:
            libs = [lib]

    _DEPS_CACHE.update({"libs": libs, "at": now, "key": key})
    return list(libs)


def install_hint(libs: Optional[list] = None) -> str:
    """把缺失的库翻译成"照抄就能修"的安装命令。"""
    libs = [lib for lib in (libs or []) if lib]
    pkgs = sorted({_LIB_APT[lib] for lib in libs if lib in _LIB_APT})
    unknown = [lib for lib in libs if lib not in _LIB_APT]

    lines = ["服务器缺少浏览器运行库（小红书内置 Chromium 依赖的系统库没装）："]
    lines.append("  " + ("、".join(libs) if libs else "未能确定具体库名，见下面的安装命令"))
    lines.append("在服务器上执行一次即可（约 1 分钟，只需一次）：")
    lines.append(f"  cd /opt/travelagent && sudo bash {_DEPS_SCRIPT}")
    if pkgs:
        lines.append("等价命令：")
        lines.append("  sudo apt-get update && sudo apt-get install -y " + " ".join(pkgs))
    if any(lib.startswith("libasound") for lib in libs):
        lines.append("提示：Ubuntu 24.04 起 libasound2 改名为 libasound2t64，装不上时换成后者。")
    if unknown:
        lines.append("（未自动识别的库可用 `apt-file search <库名>` 查对应包）")
    return "\n".join(lines)


def browser_env_issue(user_id: Optional[str] = None) -> Dict[str, Any]:
    """缺运行库时返回 {missing_libs, install_hint, browser_binary}；正常时返回 {}。"""
    libs = browser_missing_libs(user_id)
    if not libs:
        return {}
    binary = browser_binary_path(user_id)
    return {
        "missing_libs": libs,
        "install_hint": install_hint(libs),
        "browser_binary": str(binary) if binary else "",
    }


def _with_env_issue(user_id: Optional[str], payload: Dict[str, Any]) -> Dict[str, Any]:
    """给失败结果补上"缺库"诊断（顺带把提示接到 message 末尾，日志里也看得见）。"""
    try:
        issue = browser_env_issue(user_id)
    except Exception:                                       # 诊断本身绝不能再抛异常
        issue = {}
    if not issue:
        return payload
    payload = dict(payload)
    payload.update(issue)
    hint = issue.get("install_hint", "")
    message = payload.get("message") or ""
    if hint and hint not in message:
        payload["message"] = (message + "\n\n" + hint).strip()
    return payload


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
        _qr_cache.pop(_qr_cache_key(user_id), None)   # 新实例=新会话，旧二维码作废
        _status_cache.pop(_qr_cache_key(user_id), None)
        clear_scan_watch(user_id)

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
            return _with_env_issue(user_id, {
                "ok": False, "url": DEFAULT_URL, "log_tail": instance_log_tail(user_id),
                "message": f"MCP 进程启动后立即退出，日志尾部：\n{instance_log_tail(user_id)}"})
        if _http_reachable(inst.url):
            return {"ok": True, "url": inst.url, "port": inst.port, "pid": proc.pid,
                    "message": "MCP 实例已启动"}
        time.sleep(0.6)

    return _with_env_issue(user_id, {
        "ok": False, "url": inst.url, "port": inst.port, "log_tail": instance_log_tail(user_id),
        "message": (f"MCP 启动超时（{START_TIMEOUT:.0f}s）。日志尾部：\n{instance_log_tail(user_id)}\n"
                    f"提示：首次运行需下载无头浏览器（约 150MB），网络受限时会较慢；"
                    f"可用 XHS_START_TIMEOUT 调大等待时间")})


def instance_ready(user_id: str) -> bool:
    """实例进程活着且 HTTP 已经能连通（可以取二维码/检索了）。"""
    inst = instance_for(user_id, touch=False)
    if inst is None or not inst.alive():
        return False
    return _http_reachable(inst.url)


def instance_log_tail(user_id: str, lines: int = 8, max_bytes: int = 4096) -> str:
    """取该用户 MCP 实例日志的尾部（排查启动失败/登录会话结局用）。"""
    path = LOG_DIR / f"xhs-mcp-{_safe_name(user_id)}.log"
    try:
        if not path.exists():
            return ""
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""


def stop_mcp(user_id: str) -> bool:
    with _lock:
        inst = _instances.pop(_safe_name(user_id), None)
    _qr_cache.pop(_qr_cache_key(user_id), None)      # 实例没了，缓存里的码也失效
    _status_cache.pop(_qr_cache_key(user_id), None)
    clear_scan_watch(user_id)
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


def _qr_cache_key(user_id: str) -> str:
    return _safe_name(user_id)


def cached_qrcode(user_id: str, max_age: float = QR_CACHE_SECONDS) -> Optional[Dict[str, Any]]:
    """取缓存的二维码（没过期就返回，否则 None）。

    为什么要缓存：上游 issue #799 指出，重复调用 MCP 的 `get_login_qrcode`
    会**新建浏览器并取消旧会话** —— 用户在手机上刚点完"确认登录"、
    或者正卡在二次设备验证页时，再取一次码就把那个会话关掉了，
    结果就是"扫码后毫无反应、永远不变绿"。
    """
    entry = _qr_cache.get(_qr_cache_key(user_id))
    if not entry:
        return None
    if time.time() - entry["at"] > max_age:
        _qr_cache.pop(_qr_cache_key(user_id), None)
        return None
    payload = dict(entry["payload"])
    expires_at = payload.get("expires_at")
    if expires_at:
        try:
            from datetime import datetime
            if datetime.fromisoformat(expires_at).timestamp() - time.time() < 5:
                return None                      # 马上就要过期了，别给用户一张废码
        except Exception:
            pass
    payload["cached"] = True
    payload["cache_age"] = round(time.time() - entry["at"], 1)
    return payload


def login_qrcode(user_id: str, force: bool = False) -> Dict[str, Any]:
    """取登录二维码（**云上用户登录的正解**，无需桌面/弹窗，也不用碰 cookies 文件）。

    流程：确保该用户实例在跑 → 调 MCP 的 `get_login_qrcode` 工具 → 返回 Base64 图片，
    前端直接 <img src="data:image/png;base64,..."> 展示，用户用小红书 App 扫码即可。

    ⚠️ `force=False`（默认）时优先返回**缓存**的二维码：重复向 MCP 取码会取消
    当前登录会话（issue #799）。只有用户显式点「重新获取二维码」时前端才传 force=True。

    返回 {"ok", "image_base64", "mime", "text", "expires_at", "mcp_url", "message"}
    """
    from xiaohongshu_mcp_client import get_login_qrcode as _qr

    if not force:
        cached = cached_qrcode(user_id)
        if cached is not None:
            return cached

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
                return _with_env_issue(user_id, {
                    "ok": False, "pending": True, "mcp_url": url, "waited": waited,
                    "message": (started.get("message") or "小红书实例正在启动…")
                               + (f"（已等待 {waited}s）" if waited > 5 else ""),
                })
            return _with_env_issue(user_id, {
                "ok": False, "mcp_url": url,
                "message": started.get("message") or "小红书实例不可用",
                "log_tail": started.get("log_tail", "")})

    try:
        qr = _qr(url)
    except Exception as exc:
        logger.warning("获取登录二维码失败 user=%s: %s", user_id, exc)
        tail = instance_log_tail(user_id)
        return _with_env_issue(user_id, {
            "ok": False, "mcp_url": url, "log_tail": tail,
            "message": f"获取二维码失败：{exc}" + (f"\n实例日志尾部：\n{tail}" if tail else "")})

    if not qr.get("image_base64"):
        return _with_env_issue(user_id, {
            "ok": False, "mcp_url": url, "text": qr.get("text", ""),
            "message": qr.get("text") or "未取到二维码（可能已登录；如需换号请先退出登录）",
        })
    payload = {
        "ok": True,
        "mcp_url": url,
        "mime": qr.get("mime", "image/png"),
        "image_base64": qr["image_base64"],
        "text": qr.get("text", ""),
        "expires_at": qr.get("expires_at"),
        "message": qr.get("text") or "请用小红书 App 扫码登录",
        "cached": False,
        "cache_age": 0,
    }
    # 只缓存成功结果；失败/pending 不缓存，避免把故障状态粘住
    _qr_cache[_qr_cache_key(user_id)] = {"at": time.time(), "payload": dict(payload)}
    mark_scan_started(user_id)      # 进入"等扫码"状态：这期间不再调 MCP 查状态
    return payload


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
    # 换了登录态：清掉扫码等待与状态缓存，下一次查状态就按新的 cookies 判定
    clear_scan_watch(user_id)
    _status_cache.pop(_qr_cache_key(user_id), None)
    return {
        "ok": True,
        "bytes": len(data),
        "path": str(path),
        "restarted": restarted,
        "message": "cookies.json 已导入" + ("，实例已重启以生效" if restarted else "；启动实例后即可使用"),
    }


def cookie_mtime(user_id: str) -> float:
    """cookies.json 的修改时间（不存在返回 0）。"""
    try:
        return (workdir_for(user_id) / "cookies.json").stat().st_mtime
    except OSError:
        return 0.0


def _line_timestamp(line: str) -> Optional[float]:
    """解析 logrus 行里的 time="2026-09-16T16:52:42+08:00"。"""
    match = re.search(r'time="([0-9T:\-+\.]+)"', line)
    if not match:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(match.group(1)).timestamp()
    except ValueError:
        return None


def scan_log_verdict(user_id: str, since: float) -> str:
    """从实例日志里读出 MCP 对该登录会话的最新判断（不调用 MCP，因此不启浏览器）。

    MCP 会打印：
      · `等待扫码登录，会话 #N，超时 4m0s`                        → waiting
      · `扫码登录成功，cookies 已保存，会话 #N`                    → success
      · `登录会话 #N 结束，未检测到扫码（超时或已被新的二维码取代）`  → ended
    """
    text = instance_log_tail(user_id, lines=400, max_bytes=262144)
    if not text:
        return ""
    verdict = ""
    for line in text.splitlines():
        stamp = _line_timestamp(line)
        if stamp is not None and stamp < since - 5:
            continue                                     # 上一轮会话的行，忽略
        if "扫码登录成功" in line:
            verdict = "success"
        elif "结束，未检测到扫码" in line:
            verdict = "ended"
        elif "等待扫码登录" in line and verdict == "":
            verdict = "waiting"
    return verdict


def mark_scan_started(user_id: str) -> None:
    """登记"刚发出去一张二维码，用户在扫码"。登记期间不再调用 MCP 查状态。

    为什么必须这样：上游 `service.go` 的 `CheckLoginStatus` 每次调用都会
    `newBrowser()` **新起一个 Chromium**（导航 /explore + sleep 1s，实测约 4 秒），
    而登录会话要靠**另一个**浏览器活 4 分钟、每 500ms 检查扫码结果。
    云主机上每 2.5 秒轮询一次 = 不停启浏览器，既挤掉登录会话又让扫码检测失灵，
    用户看到的就是"扫码后毫无反应"。
    """
    _scan_watch[_qr_cache_key(user_id)] = {
        "issued_at": time.time(),
        "cookie_at": cookie_mtime(user_id),
    }


def clear_scan_watch(user_id: str) -> None:
    _scan_watch.pop(_qr_cache_key(user_id), None)


def scan_state(user_id: str) -> Dict[str, Any]:
    """当前是否处于"等扫码"状态，以及 MCP 日志对这一会话的判断。

    返回 {"pending": bool, "issued_at", "cookie_at", "verdict"}
    verdict: "" / "waiting" / "success" / "ended"
    """
    entry = _scan_watch.get(_qr_cache_key(user_id))
    if not entry:
        return {"pending": False, "issued_at": 0.0, "cookie_at": 0.0, "verdict": ""}

    issued_at = float(entry.get("issued_at") or 0)
    cookie_at = float(entry.get("cookie_at") or 0)

    # ① 登录成功的硬证据：cookies.json 在发码之后被 MCP 重写过（且不是 99B 占位）
    if cookie_present(user_id) and cookie_mtime(user_id) > cookie_at + 0.001:
        clear_scan_watch(user_id)
        _status_cache.pop(_qr_cache_key(user_id), None)
        return {"pending": False, "issued_at": issued_at, "cookie_at": cookie_at,
                "verdict": "success"}

    # ② 日志里的会话结局（"结束，未检测到扫码" = 这次扫码没成功）
    verdict = scan_log_verdict(user_id, issued_at)
    if verdict == "ended" or time.time() - issued_at > SCAN_WINDOW:
        clear_scan_watch(user_id)
        _status_cache.pop(_qr_cache_key(user_id), None)
        return {"pending": False, "issued_at": issued_at, "cookie_at": cookie_at,
                "verdict": verdict or "ended"}

    return {"pending": True, "issued_at": issued_at, "cookie_at": cookie_at,
            "verdict": verdict}


def cached_status(user_id: str, max_age: float) -> Optional[Dict[str, Any]]:
    entry = _status_cache.get(_qr_cache_key(user_id))
    if not entry or time.time() - entry["at"] > max_age:
        return None
    payload = dict(entry["payload"])
    payload["status_age"] = round(time.time() - entry["at"], 1)
    return payload


def _remember_status(user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """缓存状态探测结果（每次探测=一个 Chromium，必须节流）。"""
    _status_cache[_qr_cache_key(user_id)] = {"at": time.time(), "payload": dict(payload)}
    return payload


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

    # ---------- ① 等扫码期间：不打扰 MCP ----------
    state = scan_state(user_id)
    if state["verdict"] == "success":
        info.update({"logged_in": True, "login_state": "cookie_after_scan", "has_cookie": True,
                     "message": "扫码登录成功（MCP 已保存登录态）"})
        return info
    if state["pending"]:
        info.update({
            "logged_in": False,
            "waiting_scan": True,
            "scan_verdict": state["verdict"],
            "log_tail": instance_log_tail(user_id, lines=25),
            "message": "等待手机扫码并点「确认登录」…（扫码期间不查询 MCP，避免打扰登录）",
        })
        return info

    # ---------- ② 常态：走缓存，避免每次轮询都让 MCP 新起一个浏览器 ----------
    cached = cached_status(user_id, STATUS_CACHE_SECONDS)
    if cached is not None and running:
        return cached

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
        # ⚠️ 超时必须短：前端每 2.5 秒问一次，MCP 忙（每次探测都要新起 Chromium）时
        #    可能长时间不返回，用默认 120s 会把状态接口一起挂住 ——
        #    表现就是"扫码后页面毫无反应"。
        result = _batch_call("tools/call", {"name": "check_login_status"}, base_url=url,
                             max_retries=0, timeout=STATUS_TIMEOUT)
    except TimeoutError:
        info["message"] = "登录状态查询超时（MCP 正忙），稍后自动重试…"
        info["busy"] = True
        return info
    except ConnectionError:
        info["message"] = ("MCP 实例未启动" if MULTI_USER else "共享 MCP 服务未启动，请先运行 python start_mcp.py")
        return info
    except Exception as exc:
        # 查询本身失败时，用 cookies.json 兜底判断（MCP 只在登录成功后才写这个文件）
        if info["has_cookie"]:
            info.update({
                "logged_in": True,
                "login_state": "cookie_only",
                "message": "已检测到登录态文件（cookies.json）；状态查询失败，按已登录处理",
            })
            return _remember_status(user_id, info)
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
        # 排查"扫码后没反应"用：把 MCP 的原话 + 实例日志尾部一起给前端看。
        # 小红书有时会在手机确认后追加一次"设备安全验证"，需要再扫一张码；
        # 上游 MCP 尚未处理（issue #799），日志里能看到线索。
        info["log_tail"] = instance_log_tail(user_id, lines=25)
    return _remember_status(user_id, info)


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
    # 缺浏览器运行库是"启动必定失败"的硬伤：把原因和修复命令一起返回，前端直接展示
    issue = browser_env_issue(user_id)
    if issue:
        data.update(issue)
        if not data.get("logged_in") and not data.get("mcp_running"):
            data["message"] = (data.get("message") or "") + "\n" + issue["install_hint"]
    return data
