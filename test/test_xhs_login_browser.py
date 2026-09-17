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

    def __init__(self, present: set, srcs: dict, goto_ok=True, text=""):
        self.present = present
        self.srcs = srcs
        self.goto_ok = goto_ok
        self.text = text
        self.gotos = 0
        self.waits = 0
        self.filled = []

    def locator(self, selector):
        page = self

        class _Loc:
            async def count(self_inner):
                return 1 if selector in page.present else 0

        return _Loc()

    async def eval_on_selector(self, selector, _expr):
        return self.srcs.get(selector)

    async def evaluate(self, _expr):
        return self.text

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

    def _attach(self, present, srcs, cookies=None, text=""):
        self.session._page = _FakePage(present, srcs, text=text)
        self.session._ctx = _FakeContext(cookies or [])

    # ---------- 风控中间态（实测：扫码成功后可能要求短信验证，HTTP 471） ----------
    SMS_TEXT = ("登录后推荐更懂你的笔记 扫码成功 请在手机上确认 重新扫码 可用 小红书 或 微信 扫码 "
                "手机号登录 +86 获取验证码 短信验证码验证 验证码将发送至 +86 133******32 "
                "没有收到验证码？获取验证码 验证 问题反馈")

    # ⚠️ 真实登录页的完整文本（2026-09 从线上抓的）。
    #    它里面有「手机号登录」「获取验证码」——曾经因为把"获取验证码"当成短信阶段标志，
    #    导致打开登录页就被判定为"要求短信验证"、**二维码永远不返回给前端**，
    #    用户侧表现就是"扫码那条路完全走不通"。这条用例专门锁死这个回归。
    NORMAL_LOGIN_TEXT = (
        "登录后推荐更懂你的笔记 可用 小红书 或 微信 扫码 小红书如何扫码 手机号登录 +86 "
        "获取验证码 登录 我已阅读并同意《用户协议》《隐私政策》《儿童/青少年个人信息保护规则》 "
        "新用户可直接登录 创作中心 业务合作 发现 RED 直播 发布 通知 消息 登录 "
        "沪ICP备13030189号 违法不良信息举报电话：4006676810")

    async def test_normal_login_page_is_not_treated_as_sms_stage(self):
        """普通登录页（含"手机号登录/获取验证码"）必须给二维码，不能误判成短信验证。"""
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,REALQR"},
                     text=self.NORMAL_LOGIN_TEXT)
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "qr", "普通登录页被误判成短信验证 → 二维码不会下发")
        self.assertEqual(data["image_base64"], "REALQR")
        self.assertFalse(data.get("scanned"))

    async def test_sms_stage_wins_after_scan(self):
        """已扫码 + 风控要求短信验证 → 这时必须提示输验证码（二维码先让位）。"""
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,Q"},
                     text=self.SMS_TEXT)
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "verify_sms")
        self.assertEqual(data["phone"], "+86 133******32")

    async def test_sms_stage_before_scan_still_shows_qr(self):
        """还没扫码时即使页面上有风控文案，也应先把二维码给用户扫。"""
        text = self.SMS_TEXT.replace("扫码成功 请在手机上确认 重新扫码 ", "")
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,Q"}, text=text)
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "qr")

    async def test_sms_verification_state_is_detected_with_phone(self):
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,Q"}, text=self.SMS_TEXT)
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "verify_sms")
        self.assertEqual(data["phone"], "+86 133******32")
        self.assertIn("短信验证", data["message"])

    async def test_scanned_state_tells_user_to_confirm_on_phone(self):
        text = "扫码成功 请在手机上确认 重新扫码 手机号登录"
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,Q"}, text=text)
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "qr")
        self.assertTrue(data["scanned"])
        self.assertIn("确认登录", data["message"])

    async def test_submit_code_without_input_reports_inputs(self):
        self._attach(set(), {}, text=self.SMS_TEXT)

        async def _no_input():
            return None

        with patch.object(self.session, "_find_code_input", _no_input), \
             patch.object(self.session, "_visible_inputs",
                          lambda: __import__("asyncio").sleep(0, result=[{"placeholder": "验证码"}])):
            data = await self.session.submit_code("123456")
        self.assertFalse(data["ok"])
        self.assertIn("找不到验证码输入框", data["message"])

    async def test_submit_code_empty_is_rejected(self):
        data = await self.session.submit_code("   ")
        self.assertFalse(data["ok"])
        self.assertIn("验证码", data["message"])

    def test_sms_phone_extraction(self):
        self.assertEqual(lb._LiveSession._sms_phone("发到 +86 133******32"), "+86 133******32")
        self.assertEqual(lb._LiveSession._sms_phone("没有号码"), "")

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
        self.assertEqual(self.session._page.gotos, 1, "显式要求时可以立刻重开登录页")

    async def test_empty_page_waits_before_reloading(self):
        """手机确认登录的瞬间弹窗会短暂消失 —— 这时**不能**急着重载，否则打断登录。"""
        self._attach(set(), {})
        first = await self.session._snapshot(refresh_if_missing=False)
        self.assertEqual(first["state"], "waiting")
        self.assertEqual(self.session._page.gotos, 0, "第一次探测为空不应重载")
        self.assertIn("不要刷新", first["message"])

        with patch.object(lb, "EMPTY_RELOAD_AFTER", 3):
            for _ in range(3):
                await self.session._snapshot(refresh_if_missing=False)
        self.assertEqual(self.session._page.gotos, 1, "连续多轮都空才重开登录页")

    async def test_cookie_change_detects_login_without_dom(self):
        """DOM 还没更新时，靠 web_session 换新也能判定登录成功。"""
        self.session._page = _FakePage(set(), {})
        self.session._ctx = _FakeContext([{"name": "web_session", "value": "NEWVALUE"}])
        self.session._session0 = "OLDVALUE"
        data = await self.session._snapshot()
        self.assertEqual(data["state"], "logged_in")
        self.assertEqual(data["cookie_count"], 1)
        self.assertIn("cookie", data["message"])
        saved = json.loads((self.dir / "cookies.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["cookies"][0]["value"], "NEWVALUE")

    async def test_same_session_cookie_is_not_treated_as_login(self):
        self.session._page = _FakePage(set(), {})
        self.session._ctx = _FakeContext([{"name": "web_session", "value": "SAME"}])
        self.session._session0 = "SAME"
        with patch.object(lb, "EMPTY_RELOAD_AFTER", 99):
            data = await self.session._snapshot()
        self.assertEqual(data["state"], "waiting")

    async def test_stale_qr_content_triggers_reload(self):
        """同一张码长时间不变 = 页面停在"已失效"，要重开一次（否则用户干扫废码）。"""
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,SAME"})
        first = await self.session._snapshot()
        self.assertEqual(first["state"], "qr")
        self.assertEqual(self.session._page.gotos, 0)
        self.session._last_src_at = time.time() - 10_000        # 装作很久没变
        with patch.object(lb, "QR_STALE_AFTER", 60):
            await self.session._snapshot()
        self.assertEqual(self.session._page.gotos, 1, "废码应触发重开登录页")

    async def test_fresh_qr_does_not_trigger_reload(self):
        self._attach({lb.QR_SEL}, {lb.QR_SEL: "data:image/png;base64,SAME"})
        await self.session._snapshot()
        await self.session._snapshot()
        self.assertEqual(self.session._page.gotos, 0)

    def test_session_active_reports_membership(self):
        with patch.dict(lb._sessions, {"u1": object()}, clear=True):
            self.assertTrue(lb.session_active("u1"))
            self.assertFalse(lb.session_active("u2"))

    async def test_page_error_becomes_error_state(self):
        self._attach({lb.QR_SEL}, {})          # 有元素但取不到 src，且不能重开
        data = await self.session._snapshot(refresh_if_missing=True)
        self.assertIn(data["state"], ("waiting", "error"))


class DesktopViewTest(unittest.TestCase):
    """投屏（noVNC）可用性判断：装了就给出地址，没装就给出安装命令。"""

    def setUp(self):
        lb._desktop_cache.update({"at": 0.0, "data": None})

    def tearDown(self):
        lb._desktop_cache.update({"at": 0.0, "data": None})

    def test_available_when_novnc_responds(self):
        class _Resp:
            status_code = 200

        with patch("httpx.get", return_value=_Resp()), \
             patch.dict("os.environ", {"DISPLAY": ":99"}), \
             patch.object(lb, "HEADLESS_ENV", "auto"):
            data = lb.desktop_view()
        self.assertTrue(data["available"])
        self.assertTrue(data["lite"], "vnc_lite 存在时应标记为可用纯净画面")
        self.assertIn("/vnc/vnc.html", data["view_path"])
        self.assertIn("/vnc/vnc_lite.html", data["lite_path"])
        # noVNC 的 path 是相对当前页面解析的，所以这里必须是 websockify（不能带 /vnc 前缀）
        self.assertIn("path=websockify", data["view_path"])
        self.assertIn("path=websockify", data["lite_path"])
        self.assertIn("autoconnect=1", data["view_path"])
        self.assertIn("resize=scale", data["view_path"])
        # 站点根被 SPA 兜底路由占用时，前端要能把 nginx 片段给用户复制
        self.assertIn("location /vnc/", data["nginx_snippet"])
        self.assertIn(f"127.0.0.1:{data['port']}", data["nginx_snippet"])

    def test_reports_install_hint_when_unreachable(self):
        with patch("httpx.get", side_effect=RuntimeError("refused")), \
             patch.dict("os.environ", {"DISPLAY": ":99"}):
            data = lb.desktop_view()
        self.assertFalse(data["available"])
        self.assertIn("install-xhs-vnc.sh", data["hint"])

    def test_warns_when_backend_has_no_display(self):
        """后端若没跑在虚拟显示上，投屏里看不到浏览器窗口，必须提前警告。"""
        class _Resp:
            status_code = 200

        with patch("httpx.get", return_value=_Resp()), \
             patch.dict("os.environ", {}, clear=True), \
             patch.object(lb, "HEADLESS_ENV", "true"):
            data = lb.desktop_view()
        self.assertTrue(data["available"])
        self.assertIn("DISPLAY", data["warning"])

    def test_result_is_cached_briefly(self):
        class _Resp:
            status_code = 200

        with patch("httpx.get", return_value=_Resp()) as get, \
             patch.dict("os.environ", {"DISPLAY": ":99"}):
            lb.desktop_view()
            lb.desktop_view()
        # 第一次会探两个页面（vnc.html / vnc_lite.html），第二次走缓存不再探
        self.assertEqual(get.call_count, 2, "前端会频繁查，必须缓存")


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

    def test_headless_auto_rules(self):
        """auto 的规则：桌面系统一律有头；Linux 看有没有 DISPLAY；显式值优先。"""
        # 本机（Windows）有桌面 → 有头（本机开发时你能直接看到并操作浏览器窗口）
        with patch.object(lb, "HEADLESS_ENV", "auto"), \
             patch.dict("os.environ", {}, clear=True), patch.object(lb.os, "name", "nt"):
            self.assertFalse(lb._resolve_headless())
        # Linux + DISPLAY（Xvfb）→ 有头
        with patch.object(lb, "HEADLESS_ENV", "auto"), \
             patch.dict("os.environ", {"DISPLAY": ":99"}), \
             patch.object(lb.os, "name", "posix"), patch.object(lb.sys, "platform", "linux"):
            self.assertFalse(lb._resolve_headless(), "有 DISPLAY 时应用有头模式")
        # Linux 没 DISPLAY → 无头（否则根本起不来）
        with patch.object(lb, "HEADLESS_ENV", "auto"), \
             patch.dict("os.environ", {}, clear=True), \
             patch.object(lb.os, "name", "posix"), patch.object(lb.sys, "platform", "linux"):
            self.assertTrue(lb._resolve_headless())
        # 显式覆盖
        with patch.object(lb, "HEADLESS_ENV", "true"):
            self.assertTrue(lb._resolve_headless())
        with patch.object(lb, "HEADLESS_ENV", "false"):
            self.assertFalse(lb._resolve_headless())

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
