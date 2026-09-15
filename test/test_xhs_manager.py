"""小红书 MCP 多用户管理的单元测试（不会真的启动 exe、不依赖网络）。

覆盖：
  · 一人一工作目录（cookies 隔离的关键）
  · 实例端口分配不重复
  · **查状态是只读的**（前端每 5 秒轮询，绝不能因此拉起 Chromium 实例）
  · 共享模式退回单实例
  · 未登录（无 cookies）时状态为 False → 前端据此禁用「开始规划」
"""

import unittest
from unittest.mock import patch

from services import xhs_manager as xm


class WorkdirIsolationTest(unittest.TestCase):
    def setUp(self):
        self._multi = xm.MULTI_USER
        xm.MULTI_USER = True

    def tearDown(self):
        xm.MULTI_USER = self._multi

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


class InstanceRegistryTest(unittest.TestCase):
    def setUp(self):
        self._multi = xm.MULTI_USER
        xm.MULTI_USER = True
        xm._instances.clear()
        xm._ports_in_use.clear()

    def tearDown(self):
        xm._instances.clear()
        xm._ports_in_use.clear()
        xm.MULTI_USER = self._multi

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


if __name__ == "__main__":
    unittest.main()
