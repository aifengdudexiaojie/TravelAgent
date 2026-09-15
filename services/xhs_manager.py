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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("services.xhs_manager")

ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = ROOT / "xiaohongshumcp"                 # ⚠️ 目录名没有连字符
MCP_EXE = MCP_DIR / "xiaohongshu-mcp-windows-amd64.exe"
LOGIN_EXE = MCP_DIR / "xiaohongshu-login-windows-amd64.exe"
USERS_DIR = MCP_DIR / "users"
LOG_DIR = ROOT / "logs"


def _env_flag(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


MULTI_USER = _env_flag("XHS_MULTI_USER", True)
DEFAULT_URL = os.getenv("XHS_MCP_URL", "http://localhost:18060/mcp").strip()
PORT_BASE = int(os.getenv("XHS_PORT_BASE", "18100"))
MAX_INSTANCES = int(os.getenv("XHS_MAX_INSTANCES", "3"))
START_TIMEOUT = float(os.getenv("XHS_START_TIMEOUT", "45"))
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
def _exe_available() -> tuple[bool, str]:
    if not MCP_EXE.exists():
        return False, f"未找到 MCP 可执行文件：{MCP_EXE}（请从 xiaohongshu-mcp releases 下载后放到 xiaohongshumcp/）"
    return True, ""


def _http_reachable(url: str, timeout: float = 2.0) -> bool:
    import httpx
    try:
        resp = httpx.get(url, timeout=timeout)
        return resp.status_code < 500
    except Exception:
        return False


def start_mcp(user_id: str) -> Dict[str, Any]:
    """启动（或复用）用户自己的 MCP 实例。幂等。"""
    ok, err = _exe_available()
    if not ok:
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

        cmd = [str(MCP_EXE), f"-port=:{port}", f"-headless={'true' if HEADLESS else 'false'}"]
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

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if not inst.alive():
            _release_port(inst.port)
            with _lock:
                _instances.pop(inst.user_id, None)
            return {"ok": False, "url": DEFAULT_URL,
                    "message": f"MCP 进程启动后立即退出，详见日志 {log_path}"}
        if _http_reachable(inst.url):
            return {"ok": True, "url": inst.url, "port": inst.port, "pid": proc.pid,
                    "message": "MCP 实例已启动"}
        time.sleep(0.6)

    return {"ok": False, "url": inst.url, "port": inst.port,
            "message": f"MCP 启动超时（{START_TIMEOUT:.0f}s），详见日志 {log_path}"}


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
    if not LOGIN_EXE.exists():
        return {"ok": False, "message": f"未找到登录程序：{LOGIN_EXE}"}

    workdir = workdir_for(user_id)
    log_path = LOG_DIR / f"xhs-login-{_safe_name(user_id)}.log"
    LOG_DIR.mkdir(exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as log_fp:
            log_fp.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} login =====\n")
            log_fp.flush()
            subprocess.Popen([str(LOGIN_EXE)], cwd=str(workdir), stdout=log_fp, stderr=log_fp)
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


def cookie_present(user_id: str) -> bool:
    path = workdir_for(user_id) / "cookies.json"
    try:
        return path.exists() and path.stat().st_size > 100
    except OSError:
        return False


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
        info["message"] = (
            "尚未启动小红书实例：点「登录小红书」会自动启动并弹出扫码窗口"
            if not info["has_cookie"] else
            "检测到已保存的登录态，点「登录小红书」即可启动实例继续使用"
        )
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
        "exe_available": MCP_EXE.exists(),
        "login_exe_available": LOGIN_EXE.exists(),
        "workdir": str(workdir_for(user_id)),
    })
    if inst is not None:
        data["started_at"] = inst.started_at
    return data
