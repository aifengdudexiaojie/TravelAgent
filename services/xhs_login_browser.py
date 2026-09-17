"""网页扫码登录（自建）：用 Playwright 在服务器上驱动浏览器完成小红书登录
================================================================================

为什么要自己写，而不是用 MCP 的 `get_login_qrcode`？

上游（xiaohongshu-mcp v2.5.0）的实现有两个硬伤：

1. **只截一张静态二维码**（`FetchQrcodeImage` 抓 `.login-container .qrcode-img` 的 src），
   之后 4 分钟内不再更新。小红书自己的二维码约 1~2 分钟就失效（MCP 返回的
   "在 X 前扫码" 其实是**会话超时**，不是二维码有效期），用户稍慢一点扫到的是废码，
   手机没反应、服务端也检测不到 —— 用户看到的就是"扫码后毫无反应"。
2. **检测不到"二次设备安全验证"**（`.r-captcha-modal` 里会出现第二张要扫的码，
   见上游 issue #799）。风控触发时，用户扫完第一张还要扫第二张，而 MCP 永远等不到
   登录元素，`cookies` 也就永远存不下来。

本模块的做法：
  · 我们自己控制浏览器（headless 或 Xvfb 下的有头模式），**每次探测都重新读一次
    当前二维码的 src** —— 用户看到的永远是最新那张，不存在"扫到过期码"；
  · 同时检查 `.r-captcha-modal .qrcode-img`，一旦出现就把**那张**返回给前端并明确
    提示"需要二次验证，请再扫这张"，从而绕开 issue #799；
  · 登录成功的判定 = `.main-container .user .link-wrapper .channel` 出现
    （与上游一致）→ 立刻把 cookies 按 MCP 的 v2 格式写进该用户工作目录，
    下一个 MCP 工具调用就会带上它（`browser.go` 里每次新建浏览器都会 LoadCookies）。

指纹一致性：MCP 在 Linux 上会伪装成 **Windows Chrome**（`WithFingerprint("")`），
所以这里的 UA/locale/时区也照同一套设置，避免同一个 cookie 在两个不同指纹下被判风控。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("services.xhs_login_browser")

ROOT = Path(__file__).resolve().parent.parent

# 与 MCP 的伪装保持一致（Chrome/148.0.7778.215 + Windows，见其 browser.go 与日志）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/148.0.7778.215 Safari/537.36")
EXPLORE_URL = "https://www.xiaohongshu.com/explore"

LOGIN_SEL = ".main-container .user .link-wrapper .channel"      # 出现即"已登录"
QR_SEL = ".login-container .qrcode-img"                         # 常规登录二维码
CAPTCHA_SEL = ".r-captcha-modal .qrcode-img"                    # 二次设备安全验证二维码

HEADLESS_ENV = os.getenv("XHS_LOGIN_HEADLESS", "auto").strip().lower()


def _resolve_headless() -> bool:
    """有头还是无头。

    `auto`（默认）：有 DISPLAY 就用**有头**（Xvfb 下最接近官方推荐的"桌面登录"路径，
    也最不容易被风控盯上）；没有 DISPLAY 就退回无头。
    显式 true/false 则直接照办。
    """
    if HEADLESS_ENV in ("1", "true", "yes", "on"):
        return True
    if HEADLESS_ENV in ("0", "false", "no", "off"):
        return False
    return not bool(os.environ.get("DISPLAY"))


MAX_SESSIONS = int(os.getenv("XHS_LOGIN_MAX_SESSIONS", "2"))
IDLE_SECONDS = float(os.getenv("XHS_LOGIN_IDLE", "900"))
NAV_TIMEOUT_MS = int(os.getenv("XHS_LOGIN_TIMEOUT_MS", "60000"))
# 连续多少次探测"页面上什么都没有"之后，才重新打开登录页。
# ⚠️ 不能急着重载：手机点下"确认登录"的一瞬间，页面上的二维码弹窗可能正好消失，
# 这时若立刻重新加载，就会**打断正在进行中的登录** —— 用户看到的就是
# "二维码一直在刷新、却永远登不上"。所以先等几轮，并顺便看 cookie 有没有换新。
EMPTY_RELOAD_AFTER = int(os.getenv("XHS_LOGIN_RELOAD_AFTER", "6"))
# 同一张二维码内容多久没变化就当作"废码"（页面停在"已失效"上）→ 重开登录页。
# 小红书自己的码会轮换；一直不变说明它已经死了，用户再扫也没用。
QR_STALE_AFTER = float(os.getenv("XHS_LOGIN_QR_STALE", "180"))
# 扫码登录期间，每隔多少秒往日志里写一条"服务器浏览器此刻在显示什么"。
# 用户报"手机上确认了但没反应"时，这条日志往往一句就能定位（例如屏上是
# "二维码已失效" 还是 "安全验证"）。
DIAG_EVERY = float(os.getenv("XHS_LOGIN_DIAG_EVERY", "30"))

_sessions: Dict[str, "_LiveSession"] = {}
_lock = threading.RLock()
_availability: Optional[tuple] = None


# ================================================================
# 环境探测
# ================================================================
def browser_executable() -> Optional[str]:
    """优先复用 MCP 已经下载好的 Chromium（省一次 ~150MB 下载）。"""
    override = os.getenv("XHS_LOGIN_BROWSER", "").strip()
    if override and Path(override).exists():
        return override

    roots: List[Path] = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "xiaohongshu-mcp" / "browser")
    else:
        roots.append(Path.home() / ".cache" / "xiaohongshu-mcp" / "browser")

    for root in roots:
        if not root.exists():
            continue
        for name in ("chrome", "chrome.exe"):
            hits = sorted(root.glob(f"*/browser/{name}"))
            if hits:
                return str(hits[-1])
    return None


def availability() -> Dict[str, Any]:
    """Playwright 是否可用（不可用时前端自动退回 MCP 的静态二维码方案）。"""
    global _availability
    if _availability is not None:
        ok, reason, exe = _availability
        return {"ok": ok, "reason": reason, "browser": exe or ""}
    exe = browser_executable()
    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
        ok, reason = True, ""
    except Exception as exc:                                    # pragma: no cover
        ok, reason = False, f"未安装 playwright（pip install playwright）：{exc}"
    if ok and not exe:
        # 没找到复用浏览器也可以跑：Playwright 自带的 chromium 也行，但需要 playwright install
        reason = "未找到 MCP 已下载的 Chromium，将尝试使用 Playwright 自带浏览器"
    _availability = (ok, reason, exe)
    return {"ok": ok, "reason": reason, "browser": exe or ""}


# ================================================================
# cookies.json 读写（MCP v2 格式）
# ================================================================
def cookie_file(workdir: Path) -> Path:
    return workdir / "cookies.json"


def read_seed(path: Path) -> int:
    """读出文件里已绑定的指纹 seed（写入时必须保留，否则指纹会变）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, dict):
        seed = data.get("seed")
        return int(seed) if isinstance(seed, int) and seed > 0 else 0
    return 0


def normalize_cookies(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Playwright 的 cookie → MCP 期望的 NetworkCookie 字段（只留通用字段）。"""
    out: List[Dict[str, Any]] = []
    for c in raw or []:
        name, value = c.get("name"), c.get("value")
        if not name:
            continue
        item: Dict[str, Any] = {
            "name": name,
            "value": value if value is not None else "",
            "domain": c.get("domain") or ".xiaohongshu.com",
            "path": c.get("path") or "/",
        }
        expires = c.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = float(expires)
        item["httpOnly"] = bool(c.get("httpOnly"))
        item["secure"] = bool(c.get("secure"))
        same = c.get("sameSite")
        item["sameSite"] = same if same in ("Strict", "Lax", "None") else "Lax"
        out.append(item)
    return out


def write_cookies(path: Path, cookies: List[Dict[str, Any]], seed: int = 0) -> int:
    """按 MCP 的 v2 结构落盘：{"version":2,"seed":..,"saved_at":..,"cookies":[...]}。

    这样 MCP 下一次新建浏览器时会通过 `cookies.LoadCookies()` 读到它（无需重启实例）。
    """
    payload = {
        "version": 2,
        "seed": seed or read_seed(path),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cookies": normalize_cookies(cookies),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)                    # 凭据文件，权限收紧
        except OSError:
            pass
    return len(payload["cookies"])


def load_cookies_for_context(path: Path) -> List[Dict[str, Any]]:
    """把工作目录里的 cookies.json 喂给新浏览器（兼容 v1 裸数组与 v2 结构）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("cookies") if isinstance(data, dict) else data
    ctx: List[Dict[str, Any]] = []
    for c in items or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        item = {"name": c["name"], "value": c.get("value") or "",
                "domain": c.get("domain") or ".xiaohongshu.com", "path": c.get("path") or "/"}
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            item["expires"] = float(exp)
        ctx.append(item)
    return ctx


# ================================================================
# 会话：一个用户一个浏览器，所有浏览器操作都跑在它自己的事件循环线程里
# ----------------------------------------------------------------
# Playwright 的对象不能跨线程用；FastAPI 的 to_thread 每次可能是不同线程，
# 所以每个会话自带一个事件循环线程，外部只用 submit() 投递协程。
# ================================================================
class _LiveSession:
    def __init__(self, user_id: str, workdir: Path):
        self.user_id = user_id
        self.workdir = workdir
        self.started_at = time.time()
        self.last_used = time.time()
        self.last_state = ""
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever,
                                       name=f"xhs-login-{user_id[:8]}", daemon=True)
        self._thread.start()
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        # 登录**成功**的硬信号：web_session 这个 cookie 被换成新的值。
        # 只看 DOM 会在"页面还没刷新/弹窗刚关"时误判，cookie 变化才是真凭据。
        self._session0: str = ""
        self._empty_streak = 0
        # 同一张二维码内容多久没变就算"废码"（小红书自己的码会轮换；一直不变说明
        # 页面停在"二维码已失效，点击刷新"那个状态，用户再扫也没用）→ 重开一次登录页
        self._last_src = ""
        self._last_src_at = 0.0
        self._console: List[str] = []
        self._failed: List[str] = []
        self._last_diag_at = 0.0
        self.last_error = ""

    # ---------- 线程桥 ----------
    def submit(self, coro, timeout: float = 120):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    # ---------- 浏览器动作 ----------
    async def launch(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        exe = browser_executable()
        headless = _resolve_headless()
        kwargs: Dict[str, Any] = {
            "headless": headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
                "--window-size=1280,900",
                # ⚠️ 关键：在 Xvfb 上没有窗口管理器时，Chrome 可能把窗口当成"被遮挡/后台"，
                #    于是**节流页面 JS**；而小红书登录是靠页面脚本轮询/长连接拿扫码结果的，
                #    被节流就会表现为"手机已确认、页面永远不动"。这几个 flag 关掉各种节流。
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
                "--window-position=0,0",
            ],
        }
        if exe:
            kwargs["executable_path"] = exe
        self._browser = await self._pw.chromium.launch(**kwargs)
        logger.info("扫码登录浏览器已启动：user=%s headless=%s exe=%s",
                    self.user_id, headless, exe or "(playwright 自带)")
        self._ctx = await self._browser.new_context(
            user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 900},
        )
        existing = load_cookies_for_context(cookie_file(self.workdir))
        if existing:
            try:
                await self._ctx.add_cookies(existing)
                logger.info("登录浏览器已带上已有 cookies：user=%s n=%d", self.user_id, len(existing))
            except Exception as exc:
                logger.warning("带入已有 cookies 失败（忽略）：%s", exc)
        self._page = await self._ctx.new_page()
        # 页面的 console / JS 报错也收下来：登录卡住时它们经常是唯一线索
        try:
            self._page.on("console", lambda msg: self._remember_console(f"{msg.type}: {msg.text}"))
            self._page.on("pageerror", lambda exc: self._remember_console(f"pageerror: {exc}"))
            # 记录加载失败的请求：如果登录回调依赖的请求被子网/代理拦掉，页面就永远
            # 完不成登录（控制台里只会看到一句 ERR_BLOCKED_BY_CLIENT，看不出是哪个域名）
            self._page.on("requestfailed",
                          lambda req: self._remember_failed(
                              f"{req.url[:160]} → {(req.failure or '失败')}"))
        except Exception:
            pass
        await self._goto_explore()
        self._session0 = await self._web_session()      # 记录登录前的 web_session，用于对比
        self._last_src = ""                             # 上一次看到的二维码内容
        self._last_src_at = time.time()

    def _remember_console(self, line: str) -> None:
        self._console.append(line[:300])
        del self._console[:-30]                          # 只留最近 30 条

    def _remember_failed(self, line: str) -> None:
        self._failed.append(line[:220])
        del self._failed[:-20]

    async def _web_session(self) -> str:
        try:
            cookies = await self._ctx.cookies()
        except Exception:
            return ""
        for c in cookies or []:
            if c.get("name") == "web_session":
                return str(c.get("value") or "")
        return ""

    async def _cookie_login_detected(self) -> bool:
        """web_session 从旧值变成了新值 —— 说明这次登录真的落地了。"""
        if not self._session0:
            return False
        current = await self._web_session()
        return bool(current) and current != self._session0

    async def _goto_explore(self) -> None:
        try:
            await self._page.goto(EXPLORE_URL, wait_until="domcontentloaded",
                                  timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning("打开小红书首页失败：%s", exc)
        await self._page.wait_for_timeout(3000)          # 等登录弹窗渲染

    async def _count(self, selector: str) -> int:
        try:
            return await self._page.locator(selector).count()
        except Exception:
            return 0

    async def _qr_src(self, selector: str) -> Optional[str]:
        try:
            src = await self._page.eval_on_selector(selector, "el => el.src")
        except Exception:
            return None
        if not src or not isinstance(src, str):
            return None
        return src

    @staticmethod
    def _split_data_url(src: str) -> tuple:
        match = re.match(r"data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", src, re.S)
        if not match:
            return "", ""
        return match.group(1), match.group(2)

    async def _save_and_report_login(self, why: str) -> Dict[str, Any]:
        cookies = await self._ctx.cookies()
        count = write_cookies(cookie_file(self.workdir), cookies)
        logger.info("扫码登录成功（%s），已写入 cookies.json：user=%s n=%d",
                    why, self.user_id, count)
        return {"state": "logged_in", "cookie_count": count,
                "message": f"登录成功（已保存登录态，判定依据：{why}）"}

    async def _snapshot(self, refresh_if_missing: bool = False) -> Dict[str, Any]:
        """看一眼当前状态：已登录 / 需要二次验证 / 有二维码 / 什么都没有。"""
        # ① 已登录的两种证据：登录后的 DOM 元素，或 web_session 被换新
        if await self._count(LOGIN_SEL) > 0:
            return await self._save_and_report_login("页面已显示登录态")
        if await self._cookie_login_detected():
            self._empty_streak = 0
            return await self._save_and_report_login("登录 cookie 已更新")

        # ② 二次设备安全验证：小红书会弹出另一张要扫的码（上游 MCP 不处理这个）
        if await self._count(CAPTCHA_SEL) > 0:
            self._empty_streak = 0
            mime, b64 = self._split_data_url(await self._qr_src(CAPTCHA_SEL) or "")
            return {"state": "verify", "mime": mime or "image/png", "image_base64": b64,
                    "message": "小红书要求二次安全验证：请再扫这张码"}

        # ③ 常规登录二维码（实时读取，永远是当前这张）
        src = await self._qr_src(QR_SEL) if await self._count(QR_SEL) > 0 else None
        if src:
            self._empty_streak = 0
            # 同一张内容长时间不变 = 页面停在"二维码已失效"那张废码上，重开一次
            if src != self._last_src:
                self._last_src, self._last_src_at = src, time.time()
            elif time.time() - self._last_src_at > QR_STALE_AFTER:
                logger.info("二维码内容 %ds 未变化，视为已失效，重新打开登录页：user=%s",
                            int(QR_STALE_AFTER), self.user_id)
                self._last_src, self._last_src_at = "", time.time()
                await self._goto_explore()
                if await self._cookie_login_detected():
                    return await self._save_and_report_login("重载后登录 cookie 已更新")
                src = await self._qr_src(QR_SEL) if await self._count(QR_SEL) > 0 else None
            if src:
                mime, b64 = self._split_data_url(src)
                if b64:
                    return {"state": "qr", "mime": mime or "image/png", "image_base64": b64,
                            "message": "请用小红书 App 扫码，并在手机上点「确认登录」"}

        # ④ 什么都没有：先**耐心等**（手机刚确认时弹窗会短暂消失，此时重载会打断登录），
        #    连续多轮都空才重新打开登录页。
        self._empty_streak += 1
        if refresh_if_missing or self._empty_streak >= EMPTY_RELOAD_AFTER:
            logger.info("页面没有二维码/登录元素（连续 %d 次），重新打开登录页：user=%s",
                        self._empty_streak, self.user_id)
            self._empty_streak = 0
            await self._goto_explore()
            if await self._count(LOGIN_SEL) > 0:
                return await self._save_and_report_login("重载后页面显示已登录")
            if await self._cookie_login_detected():
                return await self._save_and_report_login("重载后登录 cookie 已更新")
            src = await self._qr_src(QR_SEL) if await self._count(QR_SEL) > 0 else None
            if src:
                mime, b64 = self._split_data_url(src)
                if b64:
                    return {"state": "qr", "mime": mime or "image/png", "image_base64": b64,
                            "message": "请用小红书 App 扫码，并在手机上点「确认登录」"}

        return {"state": "waiting",
                "message": "等待二维码出现…（如果手机已经确认，请稍等几秒，不要刷新页面）"}

    async def probe(self, refresh_if_missing: bool = False) -> Dict[str, Any]:
        self.last_used = time.time()
        try:
            data = await self._snapshot(refresh_if_missing=refresh_if_missing)
            self.last_error = ""
        except Exception as exc:
            logger.warning("探测登录状态失败：user=%s %s", self.user_id, exc)
            self.last_error = str(exc)
            data = {"state": "error", "message": f"浏览器操作失败：{exc}"}
        self.last_state = data.get("state", "")
        if data.get("state") not in ("logged_in",):
            await self._maybe_log_diag()
        return data

    async def _maybe_log_diag(self) -> None:
        """每隔 DIAG_EVERY 秒把"服务器浏览器此刻在显示什么"写进日志。

        用户反馈"手机上确认了但没反应"时，这一行通常就能定位问题：
        屏上是"二维码已失效"、还是"安全验证"、还是什么都没变。
        """
        now = time.time()
        if now - self._last_diag_at < DIAG_EVERY:
            return
        self._last_diag_at = now
        try:
            url = self._page.url
            title = await self._page.title()
            text = await self._page.evaluate("() => document.body.innerText || ''")
        except Exception as exc:
            logger.info("页面诊断失败：user=%s %s", self.user_id, exc)
            return
        flat = re.sub(r"\s+", " ", (text or "")).strip()
        console = " ⏐ ".join(self._console[-3:])[:300]
        failed = " ⏐ ".join(self._failed[-3:])[:400]
        logger.info("页面诊断：user=%s url=%s title=%r 二维码=%s 登录元素=%s 二次验证=%s 文本=%r 控制台=%r 加载失败=%r",
                    self.user_id, url, title,
                    await self._count(QR_SEL), await self._count(LOGIN_SEL),
                    await self._count(CAPTCHA_SEL), flat[:300], console, failed)

    async def debug_snapshot(self, with_shot: bool = True) -> Dict[str, Any]:
        """给排错用的完整快照：页面在显示什么 + 截图。

        之所以要有它：用户说"扫码没反应"时，只有看到**服务器那个浏览器**的画面，
        才能判断是二维码失效、还是小红书在要二次验证、还是页面报错。
        """
        self.last_used = time.time()
        out: Dict[str, Any] = {"state": self.last_state, "url": "", "title": "", "text": "",
                               "selectors": {}, "class_hints": [], "console": list(self._console),
                               "failed_requests": list(self._failed),
                               "web_session_changed": await self._cookie_login_detected(),
                               "image_base64": "", "mime": "image/png"}
        try:
            out["url"] = self._page.url
            out["title"] = await self._page.title()
            out["text"] = re.sub(r"\n{2,}", "\n",
                                 (await self._page.evaluate("() => document.body.innerText || ''"))[:1500])
            for name, sel in (("login", LOGIN_SEL), ("qrcode", QR_SEL), ("captcha", CAPTCHA_SEL),
                              ("login_container", ".login-container"),
                              ("captcha_modal", ".r-captcha-modal"),
                              ("any_qrcode", ".qrcode-img"),
                              ("expired_hint", "[class*=expired]")):
                out["selectors"][name] = await self._count(sel)
            out["class_hints"] = await self._page.evaluate("""() => {
                const out = [];
                for (const el of document.querySelectorAll('*')) {
                    const c = (el.className && el.className.toString) ? el.className.toString() : '';
                    if (/captcha|verify|expired|scan|qrcode|login/i.test(c)) {
                        out.push(el.tagName.toLowerCase() + '.' + c.trim().slice(0, 80));
                    }
                    if (out.length >= 25) break;
                }
                return out;
            }""")
            if with_shot:
                shot = await self._page.screenshot(type="png", full_page=False)
                out["image_base64"] = base64.b64encode(shot).decode()
        except Exception as exc:
            out["error"] = str(exc)
        return out

    async def refresh(self) -> Dict[str, Any]:
        """重新打开登录页拿一张新码。

        与 MCP 不同，这里"重新取码"是安全的：整个登录会话就是我们自己这个浏览器，
        不存在"再调一次就把上一个会话关掉"的问题。
        """
        self.last_used = time.time()
        await self._goto_explore()
        return await self.probe()

    async def close(self) -> None:
        for closer in (getattr(self._ctx, "close", None), getattr(self._browser, "close", None)):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass

    def shutdown(self) -> None:
        try:
            self.submit(self.close(), timeout=30)
        except Exception:
            pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass


def _safe(user_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", user_id or "unknown")


# ================================================================
# 对外接口（供 controller 调用，全部是阻塞式，路由里用 to_thread 包）
# ================================================================
def _workdir(user_id: str) -> Path:
    from services import xhs_manager                    # 延迟导入，避免循环依赖
    return xhs_manager.workdir_for(user_id)


def reap_idle() -> int:
    """回收闲置/超时的登录会话（避免浏览器泄漏）。"""
    now = time.time()
    killed = 0
    with _lock:
        for user_id, session in list(_sessions.items()):
            if now - session.last_used > IDLE_SECONDS:
                _sessions.pop(user_id, None)
                session.shutdown()
                killed += 1
    return killed


def session_active(user_id: str) -> bool:
    """该用户是否正在进行"实时扫码登录"（供状态接口判断：进行中就别去打扰 MCP）。"""
    with _lock:
        return user_id in _sessions


def active_sessions() -> List[Dict[str, Any]]:
    with _lock:
        return [{"user_id": s.user_id, "state": s.last_state,
                 "age": round(time.time() - s.started_at, 1),
                 "error": s.last_error} for s in _sessions.values()]


def diagnostics() -> Dict[str, Any]:
    """给前端/排错用：环境 + 每个登录会话的现状。"""
    avail = availability()
    return {
        **avail,
        "headless": _resolve_headless(),
        "display": os.environ.get("DISPLAY", ""),
        "browser": browser_executable() or "",
        "sessions": active_sessions(),
        "max_sessions": MAX_SESSIONS,
        "reload_after": EMPTY_RELOAD_AFTER,
        "hint": ("" if avail["ok"] else
                 "服务器未安装 playwright：cd /opt/travelagent && sudo bash deploy/install-xhs-login.sh"),
    }


def start(user_id: str) -> Dict[str, Any]:
    """开始/继续扫码登录：拉起浏览器并返回**当前**二维码。"""
    avail = availability()
    if not avail["ok"]:
        return {"ok": False, "state": "unavailable", "message": avail["reason"]}

    reap_idle()
    with _lock:
        session = _sessions.get(user_id)
        if session is not None:
            session.last_used = time.time()
        else:
            if len(_sessions) >= MAX_SESSIONS:
                return {"ok": False, "state": "busy",
                        "message": f"同时登录的用户过多（上限 {MAX_SESSIONS}），请稍后再试"}
            session = _LiveSession(user_id, _workdir(user_id))
            try:
                session.submit(session.launch())
            except Exception as exc:
                session.shutdown()
                logger.exception("启动登录浏览器失败：%s", exc)
                return {"ok": False, "state": "error",
                        "message": f"启动登录浏览器失败：{exc}",
                        "hint": _launch_hint()}
            _sessions[user_id] = session
            logger.info("已启动扫码登录会话：user=%s", user_id)

    return probe(user_id, refresh_if_missing=True)


def probe(user_id: str, refresh_if_missing: bool = False) -> Dict[str, Any]:
    with _lock:
        session = _sessions.get(user_id)
    if session is None:
        return {"ok": False, "state": "closed",
                "message": "登录会话已结束，请重新点「登录小红书」"}
    data = session.submit(session.probe(refresh_if_missing=refresh_if_missing))
    data["ok"] = data.get("state") in ("qr", "verify", "logged_in", "waiting")
    if data.get("state") == "logged_in":
        _note_login_success(user_id)
        with _lock:
            _sessions.pop(user_id, None)
        session.shutdown()
    return data


def refresh(user_id: str) -> Dict[str, Any]:
    with _lock:
        session = _sessions.get(user_id)
    if session is None:
        return start(user_id)
    data = session.submit(session.refresh())
    data["ok"] = True
    return data


def debug(user_id: str, with_shot: bool = True) -> Dict[str, Any]:
    """排错：返回服务器上登录浏览器的画面与页面信息。"""
    with _lock:
        session = _sessions.get(user_id)
    if session is None:
        return {"ok": False, "state": "closed",
                "message": "没有正在进行的登录会话（请先点「登录小红书」）"}
    data = session.submit(session.debug_snapshot(with_shot=with_shot), timeout=120)
    data["ok"] = True
    data["headless"] = _resolve_headless()
    return data


def stop(user_id: str) -> Dict[str, Any]:
    with _lock:
        session = _sessions.pop(user_id, None)
    if session is None:
        return {"ok": True, "message": "没有正在进行的登录会话"}
    session.shutdown()
    return {"ok": True, "message": "已关闭登录浏览器"}


def stop_all() -> None:
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.shutdown()


def _note_login_success(user_id: str) -> None:
    """登录成功后清掉 MCP 侧的缓存，让状态立刻变绿（不必再等一次 MCP 探测）。"""
    try:
        from services import xhs_manager
        xhs_manager.clear_scan_watch(user_id)
        xhs_manager.mark_logged_in(user_id)
    except Exception as exc:                             # pragma: no cover
        logger.info("标记登录成功失败（不影响登录态本身）：%s", exc)


def _launch_hint() -> str:
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        return ("无头模式下启动失败。服务器请安装虚拟显示后重试："
                "sudo bash deploy/install-xhs-login.sh（内含 Xvfb 配置）")
    return "可在 日志（logs/xhs-mcp-*.log）与后端日志里查看详细原因"
