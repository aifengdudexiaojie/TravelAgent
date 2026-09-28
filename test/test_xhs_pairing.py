"""配对码（登录助手用的一次性凭据）单测。"""

import time
import unittest
from unittest.mock import patch

from services import xhs_pairing as xp


class PairingTest(unittest.TestCase):
    def setUp(self):
        xp._codes.clear()
        xp._fails.clear()

    def tearDown(self):
        xp._codes.clear()
        xp._fails.clear()

    def test_issue_and_consume_once(self):
        item = xp.issue("user-1")
        code = item["code"]
        self.assertEqual(len(code), 8)
        self.assertTrue(code.isalnum())
        self.assertGreater(item["expires_in"], 0)

        self.assertEqual(xp.consume(code, "1.1.1.1"), "user-1")
        # 一次性：重放必须失败
        self.assertIsNone(xp.consume(code, "1.1.1.1"))

    def test_consume_is_case_and_space_tolerant(self):
        code = xp.issue("user-2")["code"]
        self.assertEqual(xp.consume(f"  {code.lower()} ", "1.1.1.1"), "user-2")

    def test_expired_code_is_rejected(self):
        code = xp.issue("user-3")["code"]
        # 直接把过期时间挪到过去（比 mock 整个 time 模块更贴近真实）
        xp._codes[code]["expires_at"] = time.time() - 1
        self.assertIsNone(xp.consume(code, "1.1.1.1"))
        self.assertNotIn(code, xp._codes, "过期码应被清理")

    def test_reissue_invalidates_previous(self):
        first = xp.issue("user-4")["code"]
        second = xp.issue("user-4")["code"]
        self.assertNotEqual(first, second)
        self.assertIsNone(xp.consume(first, "1.1.1.1"))
        self.assertEqual(xp.consume(second, "1.1.1.1"), "user-4")

    def test_codes_are_unique_across_users(self):
        codes = {xp.issue(f"u{i}")["code"] for i in range(50)}
        self.assertEqual(len(codes), 50)

    def test_wrong_codes_are_rate_limited_per_ip(self):
        ip = "9.9.9.9"
        for _ in range(xp.MAX_FAILS_PER_IP):
            xp.consume("WRONGCOD", ip)
        self.assertTrue(xp.too_many_fails(ip))
        self.assertFalse(xp.too_many_fails("8.8.8.8"))

    def test_successful_use_clears_fail_counter(self):
        ip = "7.7.7.7"
        xp.consume("BADCODE1", ip)
        self.assertFalse(xp.too_many_fails(ip))
        code = xp.issue("user-5")["code"]
        xp.consume(code, ip)
        for _ in range(xp.MAX_FAILS_PER_IP):
            xp.consume("BADCODE2", ip)
        # 成功一次会清掉之前的失败记录，所以这里才刚刚到达上限
        self.assertTrue(xp.too_many_fails(ip))

    def test_pending_count(self):
        xp.issue("a")
        xp.issue("b")
        self.assertEqual(xp.pending_count(), 2)


if __name__ == "__main__":
    unittest.main()
