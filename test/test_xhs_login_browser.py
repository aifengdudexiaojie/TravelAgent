"""网页扫码登录（自建驱动器）的单测

重点覆盖"写回 MCP 格式"这段——写错了 MCP 读不到登录态，等于没登录：
  · v2 结构 {"version":2,"seed":..,"saved_at":..,"cookies":[...]}
  · **保留文件里已有的 seed**（MCP 用它绑定浏览器指纹，必须一致）
  · Playwright 的 cookie 字段 → MCP 期望的 NetworkCookie 字段
  · v1（裸数组）与 v2 两种历史文件都能读回来喂给浏览器
另外覆盖状态机：已登录 / 二次验证 / 有码 / 什么都没有。
这里全部用假的 page/context，不启动真浏览器（真浏览器由 test/manual 下的脚本验）。
"""

import json
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from services import xhs_login_browser as lb


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


class _FakePage:
    """按"选择器 → 出现次数 / src"返回假页面。"""

    def __init__(self, present: set, srcs: dict, goto_ok=True):
        self.present = present
        self.srcs = srcs
        self.goto_ok = goto_ok
        self.gotos = 0
        self.waits = 0

    def locator(self, selector):
        page = self

        class _Loc:
            async def count(self_inner):
                return 1 if selector in page.present else 0

        return _Loc()

    async def eval_on_selector(self, selector, _expr):
        return self.srcs.get(selector)

    async def goto(self, *_a, **_k):
        self.gotos += 1
        if not self.goto_ok:
            raise RuntimeError("boom")

    async def wait_for_timeout(self, _ms):
        self.waits += 1


class CookieFormatTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "cookies.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_cookies_keeps_existing_seed_and_v2_shape(self):
        self.path.write_text(json.dumps({"version": 2, "seed": 309346597, "cookies": []}),
                             encoding="utf-8")
        n = lb.write_cookies(self.path, [
            {"name": "web_session", "value": "abc", "domain": ".xiaohongshu.com",
             "path": "/", "expires": time.time() + 3600, "httpOnly": True, "secure": True,
             "sameSite": "Lax"},
        ])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(n, 1)
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["seed"], 309346597, "必须保留原有 seed，否则 MCP 指纹会变")
        self.assertEqual(data["cookies"][0]["name"], "web_session")
        self.assertIn("saved_at", data)

    def test_write_cookies_session_cookie_omits_expires(self):
        lb.write_cookies(self.path, [
            {"name": "a1", "value": "v", "domain": ".xiaohongshu.com", "path": "/",
             "expires": -1, "httpOnly": False, "secure": False, "sameSite": "None"},
        ])
        item = json.loads(self.path.read_text(encoding="utf-8"))["cookies"][0]
        self.assertNotIn("expires", item)
        self.assertEqual(item["sameSite"], "None")

    def test_write_cookies_defaults_are_mcp_friendly(self):
        lb.write_cookies(self.path, [{"name": "webId", "value": "x"}])
        item = json.loads(self.path.read_text(encoding="utf-8"))["cookies"][0]
        self.assertEqual(item["domain"], ".xiaohongshu.com")
        self.assertEqual(item["path"], "/")
        self.assertEqual(item["sameSite"], "Lax")
        self.assertEqual(item["value"], "x")

    def test_read_cookies_accepts_v1_and_v2(self):
        self.path.write_text(json.dumps([{"name": "v1", "value": "1"}]), encoding="utf-8")
        self.assertEqual(len(lb.load_cookies_for_context(self.path)), 1)
        self.path.write_text(json.dumps({"version": 2, "cookies": [{"name": "v2", "value": "2"}]}),
                             encoding="utf-8")
        loaded = lb.load_cookies_for_context(self.path)
        self.assertEqual(loaded[0]["name"], "v2")

    def test_broken_file_is_ignored(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(lb.load_cookies_for_context(self.path), [])
        self.assertEqual(lb.read_seed(self.path), 0)


class SnapshotStateTest(unittest.IsolatedAsyncioTestCase):
    """状态机：已登录 / 二次验证 / 有二维码 / 等待。"""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.session = lb._LiveSession("u-test", self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _attach(self, present, srcs, cookies=None):
        self.session._page = _FakePage(present, srcs)
        self.session._ctx = _FakeContext(cookies or [])

    async def test_logged_in_writes_cookie_file(self):
        self._attach({lb.LOGIN_SEL}, {},
                     cookies=[{"name": "web_session", "value": "s", "domain": ".xiaohongshu.com"}])
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "logged_in")
        saved = json.loads((self.dir / "cookies.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["cookies"][0]["name"], "web_session")

    async def test_captcha_modal_wins_over_login_qr(self):
        """二次设备安全验证：要返回**那一张**码（上游 MCP 就是这里缺功能）。"""
        srcs = {
            lb.QR_SEL: "data:image/png;base64,OLDLOGINQR",
            lb.CAPTCHA_SEL: "data:image/png;base64,CAPTCHAQR",
        }
        self._attach({lb.QR_SEL, lb.CAPTCHA_SEL}, srcs)
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "verify")
        self.assertEqual(data["image_base64"], "CAPTCHAQR")
        self.assertIn("二次安全验证", data["message"])

    async def test_normal_qr_returned_as_base64(self):
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,QRDATA"})
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "qr")
        self.assertEqual(data["mime"], "image/png")
        self.assertEqual(data["image_base64"], "QRDATA")

    async def test_missing_qr_reopens_page_once(self):
        self._attach(set(), {})
        data = await self.session._snapshot(refresh_if_missing=True)
        self.assertEqual(data["state"], "waiting")
        self.assertEqual(self.session._page.gotos, 1, "弹窗没了应该重新打开一次登录页")

    async def test_missing_qr_also_reopens_without_flag(self):
        """手机上确认了、但页面 DOM 没刷新时：也必须重载一次才能判定已登录。"""
        self._attach(set(), {})
        data = await self.session._snapshot(refresh_if_missing=False)
        self.assertEqual(data["state"], "waiting")
        self.assertEqual(self.session._page.gotos, 1)
        self.assertIn("二维码", data["message"])

    async def test_page_error_becomes_error_state(self):
        self._attach({lb.QR_SEL}, {})          # 有元素但取不到 src，且不能重开
        data = await self.session._snapshot(refresh_if_missing=True)
        self.assertIn(data["state"], ("waiting", "error"))


class AvailabilityTest(unittest.TestCase):
    def test_reports_ok_when_playwright_installed(self):
        with patch.object(lb, "_availability", None):
            info = lb.availability()
        self.assertTrue(info["ok"], info)
        self.assertIn("browser", info)

    def test_reports_reason_when_playwright_missing(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError("no playwright")
            return real_import(name, *args, **kwargs)

        with patch.object(lb, "_availability", None), patch("builtins.__import__", fake_import):
            info = lb.availability()
        self.assertFalse(info["ok"])
        self.assertIn("playwright", info["reason"])

    def test_headless_auto_follows_display(self):
        with patch.object(lb, "HEADLESS_ENV", "auto"), patch.dict("os.environ", {}, clear=True):
            self.assertTrue(lb._resolve_headless())
        with patch.object(lb, "HEADLESS_ENV", "auto"), \
             patch.dict("os.environ", {"DISPLAY": ":99"}):
            self.assertFalse(lb._resolve_headless(), "有 DISPLAY 时应用有头模式")
        with patch.object(lb, "HEADLESS_ENV", "true"):
            self.assertTrue(lb._resolve_headless())

    def test_sessions_are_capped(self):
        class _Live:
            last_used = time.time()
            user_id = "u1"

            def shutdown(self):
                pass

        with patch.object(lb, "MAX_SESSIONS", 1), patch.dict(lb._sessions, {"u1": _Live()}, clear=True), \
             patch.object(lb, "availability", return_value={"ok": True, "reason": "", "browser": ""}):
            result = lb.start("u2")
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "busy")

    def test_idle_sessions_are_reaped(self):
        class _S:
            last_used = time.time() - 10_000
            user_id = "old"

            def shutdown(self):
                self.closed = True

        s = _S()
        with patch.dict(lb._sessions, {"old": s}, clear=True), patch.object(lb, "IDLE_SECONDS", 60):
            self.assertEqual(lb.reap_idle(), 1)
        self.assertTrue(getattr(s, "closed", False))
        self.assertEqual(lb._sessions, {})

    def test_probe_without_session_reports_closed(self):
        with patch.dict(lb._sessions, {}, clear=True):
            self.assertEqual(lb.probe("nobody")["state"], "closed")


if __name__ == "__main__":
    unittest.main()
