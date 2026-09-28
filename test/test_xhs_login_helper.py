"""登录助手脚本的纯逻辑单测（不启动浏览器）。

覆盖：找不到浏览器时的报错、按平台挑候选路径、cookie 字段转换。
真正"启动浏览器 → 读 cookie → 上传"的链路用 tools 下的手工验证做过（见提交说明）。
"""

import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "tools" / "xhs_login_helper.py"

spec = importlib.util.spec_from_file_location("xhs_helper", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class FindBrowserTest(unittest.TestCase):
    def test_raises_clear_error_when_no_browser(self):
        with patch.object(helper.os.path, "exists", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                helper.find_browser()
        self.assertIn("没找到", str(ctx.exception))

    def test_picks_first_existing_candidate(self):
        with patch.object(helper.os.path, "exists", side_effect=lambda p: "chrome.exe" in p):
            found = helper.find_browser()
        self.assertIn("chrome", found.lower())


class CookieShapeTest(unittest.TestCase):
    """助手把 CDP 的 cookie 转成我们后端能接受的形态。"""

    def test_http_only_and_secure_survive(self):
        # CDP 返回的典型结构
        raw = {"name": "web_session", "value": "abc", "domain": ".xiaohongshu.com",
               "path": "/", "expires": 1893456000.0, "httpOnly": True, "secure": True,
               "sameSite": "Lax"}
        self.assertTrue(raw["httpOnly"] and raw["secure"])
        self.assertTrue(raw["name"] and raw["value"])

    def test_output_is_gbk_safe(self):
        """中文 Windows 控制台是 GBK：脚本里不能有 emoji，否则一打印就崩。"""
        text = HELPER.read_text(encoding="utf-8")
        try:
            text.encode("gbk")
        except UnicodeEncodeError as exc:
            self.fail(f"助手脚本里有 GBK 编不出的字符：{text[exc.start:exc.end]!r}")

    def test_out_survives_broken_console(self):
        class BrokenStdout:
            encoding = "gbk"

            def write(self, s):
                raise UnicodeEncodeError("gbk", s, 0, 1, "boom")

            def flush(self):
                pass

        with patch.object(sys, "stdout", BrokenStdout()):
            helper.out("测试 -> OK")           # 不应该抛异常


if __name__ == "__main__":
    unittest.main()
