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
import unittest
from unittest.mock import patch

from services import xhs_manager as xm


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


if __name__ == "__main__":
    unittest.main()
