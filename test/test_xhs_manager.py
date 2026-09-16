"""小红书 MCP 多用户管理的单元测试（不会真的启动 exe、不依赖网络）。

覆盖：
  · 一人一工作目录（cookies 隔离的关键）
  · 实例端口分配不重复
  · **查状态是只读的**（前端每 5 秒轮询，绝不能因此拉起 Chromium 实例）
  · 共享模式退回单实例
  · 未登录（无 cookies）时状态为 False → 前端据此禁用「开始规划」
  · 按平台解析可执行文件（云端 Linux 上放 Windows exe 必须给出明确报错）
  · cookies.json 导入（无桌面服务器的登录方式）的内容校验
"""

import os
import pathlib
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import xhs_manager as xm


class _FakeProc:
    """假装活着的 MCP 进程：poll() 返回 None 表示"还在跑"。"""

    def poll(self):
        return None


class _UsersDirCleanup:
    """测试会调用 workdir_for() 造出用户目录，跑完自动删掉本次新增的，避免污染真实数据。"""

    def setUp(self):
        super().setUp()
        self._users_before = ({d.name for d in xm.USERS_DIR.iterdir()}
                              if xm.USERS_DIR.exists() else set())

    def tearDown(self):
        try:
            if xm.USERS_DIR.exists():
                for d in xm.USERS_DIR.iterdir():
                    if d.name not in self._users_before:
                        shutil.rmtree(d, ignore_errors=True)
        finally:
            super().tearDown()

class WorkdirIsolationTest(_UsersDirCleanup, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._multi = xm.MULTI_USER
        xm.MULTI_USER = True

    def tearDown(self):
        xm.MULTI_USER = self._multi
        super().tearDown()

    def test_each_user_gets_own_workdir(self):
        a = xm.workdir_for("user-a")
        b = xm.workdir_for("user-b")
        self.assertNotEqual(a, b)
        self.assertEqual(a.name, "user-a")
        self.assertTrue(a.exists())

    def test_workdir_name_is_sanitized(self):
        # 目录名必须安全：不能出现路径分隔符，也不能是 "." / ".."
        for raw in ("../../evil/../x y\\z", "..", ".", "", "...", "..\\..\\.."):
            path = xm.workdir_for(raw)
            self.assertEqual(path.parent.resolve(), xm.USERS_DIR.resolve())
            self.assertNotIn("/", path.name)
            self.assertNotIn("\\", path.name)
            self.assertNotIn(path.name, (".", ".."))

    def test_shared_mode_uses_mcp_root(self):
        xm.MULTI_USER = False
        self.assertEqual(xm.workdir_for("user-a"), xm.MCP_DIR)
        self.assertEqual(xm.mcp_url_for("user-a"), xm.DEFAULT_URL)


class InstanceRegistryTest(_UsersDirCleanup, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._multi = xm.MULTI_USER
        xm.MULTI_USER = True
        xm._instances.clear()
        xm._ports_in_use.clear()

    def tearDown(self):
        xm._instances.clear()
        xm._ports_in_use.clear()
        xm.MULTI_USER = self._multi
        super().tearDown()

    def test_port_allocation_is_unique(self):
        ports = {xm._allocate_port() for _ in range(5)}
        self.assertEqual(len(ports), 5)
        self.assertTrue(all(p >= xm.PORT_BASE for p in ports))

    def test_status_is_read_only(self):
        """查状态不能拉起实例，也不能刷新空闲计时。"""
        with patch.object(xm, "start_mcp") as start_mcp, \
             patch("xiaohongshu_mcp_client._batch_call") as batch:
            status = xm.status_for("user-a")

        start_mcp.assert_not_called()          # 绝不因为"看一眼状态"就起实例
        batch.assert_not_called()              # 实例没起，连 MCP 都不用调
        self.assertEqual(xm._instances, {})
        self.assertFalse(status["logged_in"])
        self.assertFalse(status["mcp_running"])
        self.assertIn("登录小红书", status["message"])

    def test_mcp_url_does_not_autostart_when_disabled(self):
        with patch.object(xm, "start_mcp") as start_mcp:
            url = xm.mcp_url_for("user-a", auto_start=False)
        start_mcp.assert_not_called()
        self.assertEqual(url, xm.DEFAULT_URL)

    def test_mcp_url_autostarts_for_analysis(self):
        fake = {"ok": True, "url": "http://localhost:18123/mcp", "port": 18123}
        with patch.object(xm, "start_mcp", return_value=fake) as start_mcp:
            url = xm.mcp_url_for("user-a", auto_start=True)
        start_mcp.assert_called_once()
        self.assertEqual(url, "http://localhost:18123/mcp")

    def test_login_status_false_without_cookie(self):
        """没有 cookies.json 时必须判定未登录（前端据此禁用攻略生成）。"""
        class _FakeProc:
            def poll(self):
                return None          # 进程活着

        xm._instances["user-a"] = xm.Instance(
            user_id="user-a", port=18099, workdir=xm.workdir_for("user-a"),
            proc=_FakeProc())

        with patch("xiaohongshu_mcp_client._batch_call",
                   return_value="❌ 未登录，请先登录"):
            status = xm.login_status("user-a")
        self.assertFalse(status["logged_in"])
        self.assertFalse(status["has_cookie"])

    def test_login_status_true_when_mcp_reports_logged_in(self):
        class _FakeProc:
            def poll(self):
                return None          # 进程活着

        xm._instances["user-a"] = xm.Instance(
            user_id="user-a", port=18099, workdir=xm.workdir_for("user-a"),
            proc=_FakeProc())

        with patch("xiaohongshu_mcp_client._batch_call",
                   return_value="✅ 已登录\n用户名: 小明"):
            status = xm.login_status("user-a")
        self.assertTrue(status["logged_in"])
        self.assertEqual(status.get("username"), "小明")

    def test_stop_unknown_user(self):
        self.assertFalse(xm.stop_mcp("nobody"))




class ExePlatformTest(unittest.TestCase):
    """按平台挑可执行文件：Windows/Linux/macOS 各取各的构建。"""

    def setUp(self):
        self._platform = xm.PLATFORM
        self._env = os.environ.pop("XHS_MCP_EXE", None)
        self._env_login = os.environ.pop("XHS_LOGIN_EXE", None)

    def tearDown(self):
        xm.PLATFORM = self._platform
        if self._env is not None:
            os.environ["XHS_MCP_EXE"] = self._env
        if self._env_login is not None:
            os.environ["XHS_LOGIN_EXE"] = self._env_login

    def test_linux_missing_binary_lists_expected_names(self):
        """Linux 上只有 Windows exe 时，必须明确说"没找到 Linux 版"，并提示目录里有 exe。"""
        xm.PLATFORM = "linux"
        win_exe = xm.MCP_DIR / "xiaohongshu-mcp-windows-amd64.exe"
        existed = win_exe.exists()
        if not existed:
            win_exe.write_bytes(b"MZ\x00\x00fake")
        try:
            path, err = xm.resolve_exe("mcp")
            self.assertIsNone(path)
            self.assertIn("linux", err.lower())
            self.assertIn("检测到这些文件", err)          # 点出"你放的是 Windows 版"
        finally:
            if not existed:
                win_exe.unlink()

    def test_windows_pe_rejected_via_env_override(self):
        """显式用 XHS_MCP_EXE 指向 PE 文件时，报错要说明"平台不对"。"""
        xm.PLATFORM = "linux"
        fake = xm.LOG_DIR / "_fake_mcp.exe"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_bytes(b"MZ\x90\x00fake-pe")
        try:
            os.environ["XHS_MCP_EXE"] = str(fake)
            path, err = xm.resolve_exe("mcp")
            self.assertIsNone(path)
            self.assertIn("Windows", err)
            self.assertIn("chmod", err)                    # 给出可执行的下一步
        finally:
            os.environ.pop("XHS_MCP_EXE", None)
            fake.unlink(missing_ok=True)

    def test_elf_binary_resolves_and_gets_x_bit(self):
        """Linux 版文件存在时应能解析，并自动补上可执行权限。"""
        if os.name == "nt":
            self.skipTest("Windows 下不校验 POSIX 可执行位")
        xm.PLATFORM = "linux"
        target = xm.MCP_DIR / "xiaohongshu-mcp-linux-amd64"
        target.write_bytes(b"\x7fELF\x02\x01\x01fake")
        target.chmod(0o644)
        try:
            path, err = xm.resolve_exe("mcp")
            self.assertEqual(path, target, err)
            self.assertTrue(os.access(target, os.X_OK))
        finally:
            target.unlink(missing_ok=True)


class ImportCookiesTest(unittest.TestCase):
    """cookies.json 导入：无桌面服务器上的登录方式。"""

    def setUp(self):
        self._multi = xm.MULTI_USER
        xm.MULTI_USER = True
        xm._instances.clear()
        self.user = "test-import-user"

    def tearDown(self):
        xm.MULTI_USER = self._multi
        xm._instances.clear()
        wd = xm.USERS_DIR / self.user
        if wd.exists():
            import shutil
            shutil.rmtree(wd, ignore_errors=True)

    def test_writes_cookie_file(self):
        # 用接近真实的体积（真实 cookies.json 约 7KB；MCP 的占位文件只有 ~99B）
        payload = '{"cookies":[' + ",".join(
            '{"name":"c%d","value":"%s"}' % (i, "x" * 40) for i in range(12)) + ']}'
        result = xm.import_cookies(self.user, payload)
        self.assertTrue(result["ok"], result)
        path = xm.workdir_for(self.user) / "cookies.json"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), payload)
        self.assertTrue(xm.cookie_present(self.user))

    def test_placeholder_cookie_is_not_treated_as_logged_in(self):
        """MCP 启动会写一个 ~99B 占位 cookies.json —— 不能当成已登录。"""
        placeholder = '{"cookies":[],"origins":[],"saved_at":"placeholder"}'
        self.assertLess(len(placeholder), xm.COOKIE_MIN_BYTES)
        path = xm.workdir_for(self.user) / "cookies.json"
        path.write_text(placeholder, encoding="utf-8")
        self.assertFalse(xm.cookie_present(self.user))

    def test_rejects_empty_and_malformed(self):
        for bad, hint in (("", "为空"), ("not json at all", "JSON"),
                          ('[{"name":"web_session","value":"abc"}]', "ok"), ("{}", "不像")):
            result = xm.import_cookies(self.user, bad)
            if hint == "ok":                      # 合法 JSON 数组 → 允许（长度也要够）
                self.assertTrue(result["ok"], result)
            else:
                self.assertFalse(result["ok"], f"{bad!r} 不该通过")
                self.assertIn(hint, result["message"])

    def test_rejects_oversized(self):
        big = '{"x":"' + "a" * (xm.COOKIE_MAX_BYTES + 10) + '"}'
        result = xm.import_cookies(self.user, big)
        self.assertFalse(result["ok"])
        self.assertIn("过大", result["message"])

    def test_restarts_running_instance(self):
        class _FakeProc:
            def poll(self):
                return None

        xm._instances[self.user] = xm.Instance(
            user_id=self.user, port=18099, workdir=xm.workdir_for(self.user),
            proc=_FakeProc())
        with patch.object(xm, "stop_mcp", return_value=True) as stop, \
             patch.object(xm, "start_mcp", return_value={"ok": True, "url": "http://x/mcp"}) as start:
            result = xm.import_cookies(self.user, '{"cookies":[]}')
        self.assertTrue(result["ok"])
        self.assertTrue(result["restarted"])
        stop.assert_called_once()
        start.assert_called_once()


class QrcodeLoginTest(_UsersDirCleanup, unittest.TestCase):
    """扫码登录：二维码解析、就绪/启动中/失败三条分支、退出登录。"""

    def setUp(self):
        super().setUp()
        self._multi = xm.MULTI_USER
        xm.MULTI_USER = True
        xm._instances.clear()
        xm._qr_cache.clear()          # 二维码缓存是模块级状态，测试之间必须隔离
        self.user = "test-qr-user"

    def tearDown(self):
        xm.MULTI_USER = self._multi
        xm._instances.clear()
        xm._qr_cache.clear()
        super().tearDown()

    # ---------- MCP 客户端：解析二维码（text + image） ----------
    def test_get_login_qrcode_parses_text_image_and_expiry(self):
        from xiaohongshu_mcp_client import get_login_qrcode

        fake = [
            {"type": "text", "text": "请用小红书 App 在 2026-09-16 15:24:04 前扫码登录 👇"},
            {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgoAAA"},
        ]
        with patch("xiaohongshu_mcp_client._batch_call_content", return_value=fake):
            qr = get_login_qrcode("http://x/mcp")

        self.assertEqual(qr["image_base64"], "iVBORw0KGgoAAA")
        self.assertEqual(qr["mime"], "image/png")
        self.assertIn("扫码登录", qr["text"])
        self.assertTrue(qr["expires_at"].startswith("2026-09-16T15:24:04"))

    def test_get_login_qrcode_without_image_reports_message(self):
        from xiaohongshu_mcp_client import get_login_qrcode

        with patch("xiaohongshu_mcp_client._batch_call_content",
                   return_value=[{"type": "text", "text": "✅ 已登录"}]):
            qr = get_login_qrcode("http://x/mcp")
        self.assertEqual(qr["image_base64"], "")
        self.assertIn("已登录", qr["message"])

    # ---------- 就绪：直接取码（也覆盖 UnboundLocalError 回归） ----------
    def test_ready_instance_returns_qrcode(self):
        class _FakeProc:
            def poll(self):
                return None

        xm._instances[self.user] = xm.Instance(
            user_id=self.user, port=18100, workdir=xm.workdir_for(self.user), proc=_FakeProc())

        with patch.object(xm, "_http_reachable", return_value=True), \
             patch("xiaohongshu_mcp_client.get_login_qrcode",
                   return_value={"text": "扫码", "image_base64": "AAAA", "mime": "image/png",
                                 "expires_at": "2026-09-16T15:24:04+08:00"}) as qr:
            result = xm.login_qrcode(self.user)

        qr.assert_called_once_with("http://localhost:18100/mcp")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["image_base64"], "AAAA")

    # ---------- 启动中：必须返回 pending（用户遇到的"一直获取中"就是这个分支） ----------
    def test_starting_instance_returns_pending_not_error(self):
        with patch.object(xm, "_http_reachable", return_value=False), \
             patch.object(xm, "start_mcp",
                          return_value={"ok": False, "pending": True, "url": "http://localhost:18101/mcp",
                                        "message": "小红书实例正在启动…"}) as start, \
             patch("xiaohongshu_mcp_client.get_login_qrcode") as qr:
            result = xm.login_qrcode(self.user)

        start.assert_called_once()
        qr.assert_not_called()                    # 还没就绪就不该去取码
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending"])
        self.assertIn("启动", result["message"])

    # ---------- 真失败：带原因 + 日志尾部 ----------
    def test_hard_failure_reports_message_and_log_tail(self):
        with patch.object(xm, "_http_reachable", return_value=False), \
             patch.object(xm, "start_mcp",
                          return_value={"ok": False, "url": xm.DEFAULT_URL,
                                        "message": "未找到 mcp 可执行文件（当前平台：linux）",
                                        "log_tail": "line1\nline2"}):
            result = xm.login_qrcode(self.user)

        self.assertFalse(result["ok"])
        self.assertFalse(result.get("pending"))
        self.assertIn("linux", result["message"])
        self.assertEqual(result["log_tail"], "line1\nline2")

    # ---------- 退出登录（换号） ----------
    def test_clear_login_stops_instance_and_deletes_cookie(self):
        path = xm.workdir_for(self.user) / "cookies.json"
        path.write_text('{"cookies":[' + "x" * 600 + "]}", encoding="utf-8")
        self.assertTrue(xm.cookie_present(self.user))

        with patch.object(xm, "stop_mcp", return_value=True) as stop:
            result = xm.clear_login(self.user)

        self.assertTrue(result["ok"])
        self.assertTrue(result["removed"])
        self.assertFalse(path.exists())
        stop.assert_called_once()


class BrowserDepsDiagnosticsTest(_UsersDirCleanup, unittest.TestCase):
    """云服务器上最常见的启动失败：Chromium 缺系统库（libatk-1.0.so.0 等）。

    这类报错必须被翻译成"缺哪些库 + 复制粘贴就能修的命令"，否则用户只能看到
    一行 "error while loading shared libraries" 干瞪眼。
    """

    LAUNCH_ERROR = (
        "[launcher] Failed to launch the browser, the doc might help "
        "https://go-rod.github.io/#/compatibility?id=os: "
        "/home/travelagent/.cache/xiaohongshu-mcp/browser/148.0.7778.215/browser/chrome: "
        "error while loading shared libraries: libatk-1.0.so.0: cannot open shared object file: "
        "No such file or directory"
    )

    def setUp(self):
        super().setUp()
        xm._DEPS_CACHE.update({"libs": None, "at": 0.0, "key": ""})

    def tearDown(self):
        xm._DEPS_CACHE.update({"libs": None, "at": 0.0, "key": ""})
        super().tearDown()

    # ---------- 从报错文本里抠出 .so 名 ----------
    def test_detect_missing_lib_from_server_error(self):
        self.assertEqual(xm.detect_missing_lib(self.LAUNCH_ERROR), "libatk-1.0.so.0")
        self.assertEqual(xm.detect_missing_lib("一切正常"), "")
        self.assertEqual(xm.detect_missing_lib(""), "")

    # ---------- .so → apt 包名 + 可复制的修复命令 ----------
    def test_install_hint_maps_libs_to_apt_packages(self):
        hint = xm.install_hint(["libatk-1.0.so.0", "libnss3.so"])
        self.assertIn("libatk-1.0.so.0", hint)
        self.assertIn("install-xhs-deps.sh", hint)          # 一键脚本
        self.assertIn("libatk1.0-0", hint)                  # 包名映射
        self.assertIn("libnss3", hint)
        self.assertIn("sudo apt-get update", hint)

    def test_install_hint_warns_about_ubuntu_24_naming(self):
        hint = xm.install_hint(["libasound.so.2"])
        self.assertIn("libasound2t64", hint)

    def test_install_hint_handles_unknown_lib(self):
        hint = xm.install_hint(["libweird.so.9"])
        self.assertIn("apt-file", hint)

    # ---------- ldd 探测（Linux）+ 缓存 ----------
    def test_browser_missing_libs_parses_ldd_output_and_caches(self):
        ldd_out = SimpleNamespace(stdout=(
            "\tlinux-vdso.so.1 (0x00007ffd)\n"
            "\tlibatk-1.0.so.0 => not found\n"
            "\tlibnss3.so => not found\n"
            "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)\n"), returncode=0)
        with patch.object(xm, "PLATFORM", "linux"), \
             patch.object(xm, "browser_binary_path", return_value=pathlib.Path("/home/u/.cache/chrome")), \
             patch.object(xm.subprocess, "run", return_value=ldd_out) as run:
            libs = xm.browser_missing_libs("u")
            again = xm.browser_missing_libs("u")            # 60s 内命中缓存

        self.assertEqual(libs, ["libatk-1.0.so.0", "libnss3.so"])
        self.assertEqual(again, libs)
        self.assertEqual(run.call_count, 1, "轮询状态不能每次都 fork ldd")

    def test_browser_missing_libs_skips_ldd_when_not_linux(self):
        with patch.object(xm, "PLATFORM", "win32"), \
             patch.object(xm.subprocess, "run") as run:
            self.assertEqual(xm.browser_missing_libs("u"), [])
        run.assert_not_called()

    def test_browser_missing_libs_falls_back_to_instance_log(self):
        with patch.object(xm, "PLATFORM", "linux"), \
             patch.object(xm, "browser_binary_path", return_value=None), \
             patch.object(xm, "instance_log_tail", return_value=self.LAUNCH_ERROR):
            self.assertEqual(xm.browser_missing_libs("u"), ["libatk-1.0.so.0"])

    def test_browser_env_issue_empty_when_deps_ok(self):
        with patch.object(xm, "PLATFORM", "linux"), \
             patch.object(xm, "browser_binary_path", return_value=None), \
             patch.object(xm, "instance_log_tail", return_value="all good"):
            self.assertEqual(xm.browser_env_issue("u"), {})

    # ---------- 浏览器路径：优先用日志里那句 using browser binary ----------
    def test_browser_binary_path_prefers_log_line(self):
        tmp = pathlib.Path(tempfile.mkdtemp()) / "chrome"
        tmp.write_text("x", encoding="utf-8")
        try:
            with patch.object(xm, "instance_log_tail",
                              return_value=f"[launcher] using browser binary: {tmp}\n"):
                self.assertEqual(xm.browser_binary_path("u"), tmp)
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)

    # ---------- 取码失败时把修复命令带给前端 ----------
    def test_qr_hard_failure_carries_install_hint(self):
        issue = {"missing_libs": ["libatk-1.0.so.0"],
                 "install_hint": "在服务器上执行一次即可：\n  cd /opt/travelagent && sudo bash deploy/install-xhs-deps.sh",
                 "browser_binary": "/x/chrome"}
        with patch.object(xm, "MULTI_USER", True), \
             patch.object(xm, "_http_reachable", return_value=False), \
             patch.object(xm, "start_mcp", return_value={"ok": False, "message": "MCP 启动超时"}), \
             patch.object(xm, "browser_env_issue", return_value=issue):
            result = xm.login_qrcode("test-qr-user")

        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_libs"], ["libatk-1.0.so.0"])
        self.assertIn("install-xhs-deps.sh", result["install_hint"])
        self.assertIn("install-xhs-deps.sh", result["message"])   # 日志/接口里都看得见

    def test_status_endpoint_exposes_install_hint(self):
        issue = {"missing_libs": ["libatk-1.0.so.0"], "install_hint": "sudo bash deploy/install-xhs-deps.sh",
                 "browser_binary": ""}
        with patch.object(xm, "MULTI_USER", True), \
             patch.object(xm, "login_status", return_value={"logged_in": False, "mcp_running": False,
                                                            "message": "尚未启动小红书实例"}):
            with patch.object(xm, "browser_env_issue", return_value=issue):
                data = xm.status_for("test-qr-user")
        self.assertEqual(data["missing_libs"], ["libatk-1.0.so.0"])
        self.assertIn("install-xhs-deps.sh", data["message"])

    def test_with_env_issue_leaves_payload_alone_when_ok(self):
        payload = {"ok": False, "message": "原始原因"}
        with patch.object(xm, "browser_env_issue", return_value={}):
            self.assertIs(xm._with_env_issue("u", payload), payload)


class ScanStuckRegressionTest(_UsersDirCleanup, unittest.TestCase):
    """排查"扫码后毫无反应"时发现的两个真 bug（上游 issue #799 相关）：

    ① 重复向 MCP 取码会新建浏览器、取消旧会话 → 用户刚在手机上确认的登录被顶掉。
       所以默认必须返回**缓存**的码，只有显式 refresh 才真去取。
    ② 查登录状态的 MCP 调用默认 120s 超时，而前端每 2.5s 轮询一次 →
       MCP 一忙，状态接口就被挂住，页面看起来完全没反应。
    """

    def setUp(self):
        super().setUp()
        self._multi = xm.MULTI_USER
        xm.MULTI_USER = True
        xm._instances.clear()
        xm._qr_cache.clear()
        self.user = "test-scan-user"
        xm._instances[self.user] = xm.Instance(
            user_id=self.user, port=18123, workdir=xm.workdir_for(self.user), proc=_FakeProc())

    def tearDown(self):
        xm.MULTI_USER = self._multi
        xm._instances.clear()
        xm._qr_cache.clear()
        super().tearDown()

    def _qr_payload(self, expires_at="2099-01-01T00:00:00+08:00"):
        return {"text": "请用小红书 App 扫码登录", "image_base64": "AAAA",
                "mime": "image/png", "expires_at": expires_at}

    # ---------- ① 二维码缓存：不重复取码 ----------
    def test_second_qrcode_request_returns_cache_without_calling_mcp(self):
        with patch.object(xm, "_http_reachable", return_value=True), \
             patch("xiaohongshu_mcp_client.get_login_qrcode",
                   return_value=self._qr_payload()) as qr:
            first = xm.login_qrcode(self.user)
            second = xm.login_qrcode(self.user)          # 前端轮询/重开弹窗

        qr.assert_called_once()                          # ⚠️ 只允许取一次
        self.assertTrue(first["ok"])
        self.assertFalse(first["cached"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["image_base64"], "AAAA")

    def test_force_refresh_calls_mcp_again(self):
        with patch.object(xm, "_http_reachable", return_value=True), \
             patch("xiaohongshu_mcp_client.get_login_qrcode",
                   return_value=self._qr_payload()) as qr:
            xm.login_qrcode(self.user)
            forced = xm.login_qrcode(self.user, force=True)

        self.assertEqual(qr.call_count, 2)
        self.assertFalse(forced["cached"])

    def test_expired_cached_qrcode_is_not_returned(self):
        xm._qr_cache[xm._qr_cache_key(self.user)] = {
            "at": time.time(),
            "payload": {"ok": True, "image_base64": "OLD", "expires_at": "2020-01-01T00:00:00+08:00"},
        }
        with patch.object(xm, "_http_reachable", return_value=True), \
             patch("xiaohongshu_mcp_client.get_login_qrcode",
                   return_value=self._qr_payload()) as qr:
            result = xm.login_qrcode(self.user)

        qr.assert_called_once()                          # 过期码不能给用户，重新取
        self.assertEqual(result["image_base64"], "AAAA")

    def test_cache_dropped_when_instance_stops(self):
        xm._qr_cache[xm._qr_cache_key(self.user)] = {
            "at": time.time(), "payload": {"ok": True, "image_base64": "AAAA", "expires_at": None}}
        self.assertIsNotNone(xm.cached_qrcode(self.user))
        xm.stop_mcp(self.user)                           # 实例没了 → 旧码失效
        self.assertIsNone(xm.cached_qrcode(self.user))

    # ---------- ② 状态查询不能挂死 ----------
    def test_status_uses_short_timeout_and_reports_busy(self):
        with patch("xiaohongshu_mcp_client._batch_call",
                   side_effect=TimeoutError("slow")) as call:
            info = xm.login_status(self.user)

        self.assertFalse(info["logged_in"])
        self.assertTrue(info["busy"])
        self.assertIn("超时", info["message"])
        self.assertEqual(call.call_args.kwargs.get("timeout"), xm.STATUS_TIMEOUT)
        self.assertLessEqual(xm.STATUS_TIMEOUT, 15, "轮询接口的超时必须短")

    def test_status_falls_back_to_cookie_when_probe_fails(self):
        (xm.workdir_for(self.user) / "cookies.json").write_text(
            '{"cookies":[' + "x" * 600 + "]}", encoding="utf-8")
        with patch("xiaohongshu_mcp_client._batch_call", side_effect=RuntimeError("boom")):
            info = xm.login_status(self.user)

        self.assertTrue(info["logged_in"], "有登录态文件时不应把用户判成未登录")
        self.assertEqual(info["login_state"], "cookie_only")

    def test_status_failure_without_cookie_reports_reason(self):
        with patch("xiaohongshu_mcp_client._batch_call", side_effect=RuntimeError("boom")):
            info = xm.login_status(self.user)
        self.assertFalse(info["logged_in"])
        self.assertIn("boom", info["message"])

    def test_not_logged_in_status_includes_mcp_raw_and_log_tail(self):
        with patch("xiaohongshu_mcp_client._batch_call",
                   return_value={"content": [{"type": "text", "text": "❌ 未登录"}]}), \
             patch.object(xm, "instance_log_tail", return_value="[launcher] waiting for scan"):
            info = xm.login_status(self.user)

        self.assertFalse(info["logged_in"])
        self.assertIn("未登录", info["raw"])
        self.assertIn("waiting for scan", info["log_tail"])

    # ---------- ③ 路由：只有显式 refresh 才传 force ----------
    def test_qrcode_route_passes_refresh_flag(self):
        import asyncio

        from controller import xhs_routes

        with patch.object(xhs_routes.xhs_manager, "login_qrcode",
                          return_value={"ok": True}) as qr:
            asyncio.run(xhs_routes.xhs_qrcode(refresh=1, current_user={"user_id": "u"}))
            asyncio.run(xhs_routes.xhs_qrcode(current_user={"user_id": "u"}))

        self.assertEqual(qr.call_args_list[0].args, ("u", True))
        self.assertEqual(qr.call_args_list[1].args, ("u", False))


if __name__ == "__main__":
    unittest.main()
