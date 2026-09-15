"""意图识别日期处理的单元测试（纯函数，不调 LLM）。

回归背景：`skills/Intent.md` 曾把"当前日期"写死成 2026-07-23，导致
"明天出发" 永远返回 2026-07-24 这种**固定日期**（真实日期已是 2026-09-15）。
现在：skill 用 `{{CURRENT_DATE}}` 占位符 + 运行时注入，并且日期由下面的
纯 Python 规则归一化，不依赖模型算术。
"""

import datetime as dt
import unittest

from services.intent_dates import (
    normalize_intent_dates,
    parse_relative_end,
    parse_relative_start,
    skill_vars,
    today,
)
from utils.skill_loader import load_skill, render_template

# 固定基准：2026-09-15（周二），便于断言
BASE = dt.date(2026, 9, 15)


class RelativeStartTest(unittest.TestCase):
    def test_relative_days(self):
        self.assertEqual(parse_relative_start("今天去", BASE), dt.date(2026, 9, 15))
        self.assertEqual(parse_relative_start("明天出发去成都玩3天", BASE), dt.date(2026, 9, 16))
        self.assertEqual(parse_relative_start("后天去西安", BASE), dt.date(2026, 9, 17))
        self.assertEqual(parse_relative_start("大后天", BASE), dt.date(2026, 9, 18))

    def test_weekend_and_next_week(self):
        self.assertEqual(parse_relative_start("这周末去杭州", BASE), dt.date(2026, 9, 19))
        # "下周末" 不能被"周末"分支先吃掉
        self.assertEqual(parse_relative_start("下周末", BASE), dt.date(2026, 9, 26))
        self.assertEqual(parse_relative_start("下周三出发", BASE), dt.date(2026, 9, 23))
        self.assertEqual(parse_relative_start("下周一", BASE), dt.date(2026, 9, 21))

    def test_next_month(self):
        self.assertEqual(parse_relative_start("下个月去西安玩5天", BASE), dt.date(2026, 10, 1))
        self.assertEqual(parse_relative_start("下个月3号", BASE), dt.date(2026, 10, 3))

    def test_month_day_rolls_to_next_year_when_passed(self):
        # 今天 9-15，说"8月3日"指的是明年
        self.assertEqual(parse_relative_start("8月3日去杭州玩4天", BASE), dt.date(2027, 8, 3))
        self.assertEqual(parse_relative_start("去年8月3日去过的厦门", BASE), dt.date(2025, 8, 3))
        # 尚未到来的月份就是今年
        self.assertEqual(parse_relative_start("10月1日去北京", BASE), dt.date(2026, 10, 1))

    def test_holidays(self):
        self.assertEqual(parse_relative_start("国庆去成都", BASE), dt.date(2026, 10, 1))
        self.assertEqual(parse_relative_start("今年国庆去北京", BASE), dt.date(2026, 10, 1))
        self.assertEqual(parse_relative_start("五一", BASE), dt.date(2027, 5, 1))

    def test_explicit_iso(self):
        self.assertEqual(parse_relative_start("2026-12-24 出发", BASE), dt.date(2026, 12, 24))

    def test_no_guess(self):
        for text in ("随便什么时候", "看看有什么推荐", "帮我规划一下"):
            self.assertIsNone(parse_relative_start(text, BASE))


class RelativeEndTest(unittest.TestCase):
    def test_end_of_trip_phrasings(self):
        self.assertEqual(parse_relative_end("10月5日回来", BASE), dt.date(2026, 10, 5))
        self.assertEqual(parse_relative_end("10月5日回家，玩3天", BASE), dt.date(2026, 10, 5))
        self.assertEqual(parse_relative_end("我10月5日返程", BASE), dt.date(2026, 10, 5))

    def test_relative_end(self):
        self.assertEqual(parse_relative_end("明天回来", BASE), dt.date(2026, 9, 16))
        self.assertEqual(parse_relative_end("后天返程", BASE), dt.date(2026, 9, 17))

    def test_plain_date_is_not_end(self):
        self.assertIsNone(parse_relative_end("10月5日去杭州", BASE))


class NormalizeTest(unittest.TestCase):
    def test_overrides_stale_model_dates(self):
        """核心回归：模型基于写死基准给出的日期必须被真实日期覆盖。"""
        intent = {"days": "3天", "start_date": "2026-07-24", "end_date": "2026-07-26"}
        out = normalize_intent_dates(dict(intent), "明天出发去成都玩3天", BASE)
        self.assertEqual(out["start_date"], "2026-09-16")
        self.assertEqual(out["end_date"], "2026-09-18")       # start + 3 - 1
        self.assertEqual(out["days"], "3天")

    def test_end_is_start_plus_days_minus_one(self):
        out = normalize_intent_dates({"days": "5天", "start_date": "2026-10-01", "end_date": None},
                                     "10月1日去北京玩5天", BASE)
        self.assertEqual((out["start_date"], out["end_date"]), ("2026-10-01", "2026-10-05"))

    def test_days_from_start_and_end(self):
        out = normalize_intent_dates({"days": None, "start_date": "2026-10-01",
                                      "end_date": "2026-10-04"}, "10月1日到10月4日", BASE)
        self.assertEqual(out["days"], "4天")

    def test_start_from_end_and_days(self):
        out = normalize_intent_dates({"days": "3天", "start_date": None, "end_date": None},
                                     "10月5日回家，玩3天", BASE)
        self.assertEqual((out["start_date"], out["end_date"]), ("2026-10-03", "2026-10-05"))

    def test_no_dates_when_text_has_none(self):
        out = normalize_intent_dates({"days": "4天", "start_date": None, "end_date": None},
                                     "去杭州玩4天", BASE)
        self.assertIsNone(out["start_date"])
        self.assertIsNone(out["end_date"])

    def test_invalid_values_are_cleared(self):
        out = normalize_intent_dates({"days": None, "start_date": "瞎写的", "end_date": "2026/10/1"},
                                     "随便", BASE)
        self.assertIsNone(out["start_date"])
        self.assertIsNone(out["end_date"])

    def test_inverted_range_is_fixed(self):
        out = normalize_intent_dates({"days": "3天", "start_date": "2026-10-05",
                                      "end_date": "2026-10-01"}, "玩3天", BASE)
        self.assertEqual(out["start_date"], "2026-10-05")
        self.assertEqual(out["end_date"], "2026-10-07")


class SkillTemplateTest(unittest.TestCase):
    def test_placeholder_rendered(self):
        body = load_skill("Intent", variables={"CURRENT_DATE": "2026-09-15",
                                               "WEEKDAY": "星期二"})
        self.assertIn("2026-09-15", body)
        self.assertNotIn("{{CURRENT_DATE}}", body)

    def test_unknown_placeholder_kept(self):
        self.assertEqual(render_template("今天 {{CURRENT_DATE}} / {{NOPE}}",
                                         {"CURRENT_DATE": "2026-09-15"}),
                         "今天 2026-09-15 / {{NOPE}}")

    def test_skill_has_no_hardcoded_today_anchor(self):
        """回归：skill 正文里不允许再出现"当前日期 **YYYY-MM-DD**"这种写死的基准。"""
        raw = load_skill("Intent")
        self.assertNotRegex(
            raw, r"当前日期\s*\*\*\s*20\d\d-\d\d-\d\d",
            "skills/Intent.md 又把当前日期写死了：请改用 {{CURRENT_DATE}} 占位符",
        )

    def test_skill_vars_shape(self):
        v = skill_vars(BASE)
        self.assertEqual(v["CURRENT_DATE"], "2026-09-15")
        self.assertEqual(v["TOMORROW"], "2026-09-16")
        self.assertEqual(v["WEEKDAY"], "星期二")
        self.assertEqual(v["NEXT_MONTH_FIRST"], "2026-10-01")

    def test_today_uses_business_timezone(self):
        # 只要求是 date 且与本地日期相差不超过 1 天（时区边界）
        self.assertLessEqual(abs((today() - dt.date.today()).days), 1)


if __name__ == "__main__":
    unittest.main()
