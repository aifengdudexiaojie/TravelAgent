"""意图识别的日期处理：运行时基准确认 + 确定性归一化
================================================================================

**问题背景**：`skills/Intent.md` 里曾经把"当前日期"写死成 `2026-07-23`，于是模型
永远以那一天为基准推算相对日期 —— 用户问"明天出发"，返回的却是写死基准的次日
（实测：真实日期 2026-09-15，"明天" 被算成 2026-07-24）。前端展示的自然就是"固定日期"。

**修法（两层）**：
1. 运行时注入真实当前日期：skill 里的 `{{CURRENT_DATE}}` 等占位符在每次请求时替换，
   同时把 `current_date` 放进用户消息里，模型不必（也不该）自己猜"今天几号"；
2. 代码侧兜底：本模块用**纯 Python 规则**解析常见中文相对日期并做一致性归一化
   （`end_date = start_date + days - 1` 等），不依赖模型的算术能力。

职责：
    skill_vars()             → 给 skill 模板的占位符取值
    parse_relative_start()   → 从用户文本解析"出发日期"（能解析才返回）
    normalize_intent_dates() → 归一化 intent 里的 start_date / end_date / days
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Optional

# 业务默认时区（与 .env 的 TZ / 部署机时区保持一致）
TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")

_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def today() -> dt.date:
    """业务时区的"今天"（Asia/Shanghai）。"""
    return dt.datetime.now(TZ).date()


def skill_vars(reference: Optional[dt.date] = None) -> Dict[str, str]:
    """skill 模板占位符取值（用于 load_skill 的 {{KEY}} 替换）。"""
    d = reference or today()
    return {
        "CURRENT_DATE": d.isoformat(),
        "CURRENT_DATETIME": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "CURRENT_YEAR": str(d.year),
        "CURRENT_MONTH": str(d.month),
        "WEEKDAY": _WEEKDAY_CN[d.weekday()],
        "TOMORROW": (d + dt.timedelta(days=1)).isoformat(),
        "NEXT_MONTH_FIRST": (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1).isoformat(),
    }


# ================================================================
# 中文相对日期解析（只覆盖常见说法；解析不出来就交给模型/留空）
# ================================================================
def _cn_int(text: str) -> Optional[int]:
    """中文数字 → int（支持 一~十二 与阿拉伯数字）。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in _CN_NUM:
        return _CN_NUM[text]
    if text == "十":
        return 10
    m = re.fullmatch(r"十([一二三四五六七八九])", text)
    if m:
        return 10 + _CN_NUM[m.group(1)]
    return None


def _this_weekend(today_d: dt.date) -> dt.date:
    """本周六（今天是周日则算下一个周六）。"""
    days_ahead = 5 - today_d.weekday()          # 周一=0 … 周六=5
    if days_ahead < 0:
        days_ahead += 7
    return today_d + dt.timedelta(days=days_ahead)


def parse_relative_start(text: str, reference: Optional[dt.date] = None) -> Optional[dt.date]:
    """从用户输入里解析"出发日期"。

    支持：今天/明天/后天/大后天、这周末、下周X/下周一、下个月/下月、
          N月N日（含"明年/今年"前缀）、M/D、YYYY-MM-DD、去年（用于回看历史行程）。
    解析不出来返回 None（**不猜**）。
    """
    if not text:
        return None
    t = text.strip()
    base = reference or today()

    # 已经是明确日期：2026-10-01 / 2026/10/1
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", t)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # 相对天数
    if "大后天" in t:
        return base + dt.timedelta(days=3)
    if "后天" in t:
        return base + dt.timedelta(days=2)
    if "明天" in t or "明日" in t:
        return base + dt.timedelta(days=1)
    if "今天" in t or "今日" in t or "当天" in t:
        return base

    # 周末（注意：必须先判断"下周末"，否则会被"周末"分支先匹配掉）
    if "下周末" in t or "下个周末" in t:
        return _this_weekend(base) + dt.timedelta(days=7)
    if re.search(r"(这|本|这个)?周末", t):
        return _this_weekend(base)

    # 下周X / 下周一
    m = re.search(r"下(?:个)?(?:周|星期|礼拜)([一二三四五六日天])?", t)
    if m:
        target = m.group(1)
        weekday = 0 if target in (None, "", "一") else (6 if target in ("日", "天") else _CN_NUM.get(target, 1) - 1)
        days_ahead = (7 - base.weekday()) + weekday    # 下周一
        return base + dt.timedelta(days=days_ahead)

    # 下个月 / 下月
    if re.search(r"下(?:个)?月", t):
        first_next = (base.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        m = re.search(r"下(?:个)?月\s*(\d{1,2}|[一二三四五六七八九十]+)\s*[号日]", t)
        if m:
            day = _cn_int(m.group(1))
            if day:
                try:
                    return first_next.replace(day=day)
                except ValueError:
                    return first_next
        return first_next

    # 固定公历节日（春节等农历节日不在此处解析，交给模型按注入的年份处理）
    for keywords, (month, day) in (
        (("国庆", "十一"), (10, 1)),
        (("五一",), (5, 1)),
        (("元旦",), (1, 1)),
        (("清明",), (4, 4)),
    ):
        if any(k in t for k in keywords):
            year = base.year
            if "明年" in t:
                year += 1
            elif "去年" in t:
                year -= 1
            elif "今年" not in t and dt.date(year, month, day) < base:
                year += 1
            return dt.date(year, month, day)

    # N月N日 / N月N号（可带"明年/今年"）
    m = re.search(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*(\d{1,2}|[一二三四五六七八九十]{1,3})?\s*[号日]?", t)
    if m:
        month = _cn_int(m.group(1))
        day = _cn_int(m.group(2)) if m.group(2) else 1
        if month and 1 <= month <= 12 and day and 1 <= day <= 31:
            year = base.year
            if "明年" in t:
                year += 1
            elif "去年" in t:
                year -= 1
            elif "今年" not in t:
                # 该月日已过 → 视为明年（"8月3日" 在今天之后就不会跳到明年）
                try:
                    if dt.date(year, month, day) < base:
                        year += 1
                except ValueError:
                    return None
            try:
                return dt.date(year, month, day)
            except ValueError:
                return None

    # 仅"去年/今年/明年"这类年份词，落到该年 1 月 1 日没有意义 → 不猜
    return None


# ================================================================
# 归一化：让 start_date / end_date / days 三者在代码里保持一致
# ================================================================
def parse_relative_end(text: str, reference: Optional[dt.date] = None) -> Optional[dt.date]:
    """解析"返程/结束"日期（如"10月5日回来"、"8月6号结束"）。

    "我 10 月 5 日回来，玩 3 天" 里的 10-05 是**结束日**而不是出发日，
    交给 parse_relative_start() 会算错方向，所以单独识别一次。
    """
    if not text:
        return None
    t = text.strip()
    base = reference or today()

    # 相对说法 + 结束类动词："明天回来"、"后天返程"
    m = re.search(r"(大后天|后天|明天|明日|今天|今日)\s*"
                  r"(结束|回来|返回|返程|回程|收假|回家|回北京|回上海)", t)
    if m:
        offset = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "今天": 0, "今日": 0}[m.group(1)]
        return base + dt.timedelta(days=offset)

    # 日期 + 结束类动词
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?\s*"
                  r"(结束|回来|返回|返程|回程|收假|回家|回北京|回上海)", t)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.search(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*"
                  r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号]?\s*"
                  r"(结束|回来|返回|返程|回程|收假|回家)", t)
    if m:
        month, day = _cn_int(m.group(1)), _cn_int(m.group(2))
        if month and day:
            year = base.year
            if "明年" in t:
                year += 1
            elif "去年" in t:
                year -= 1
            elif "今年" not in t and dt.date(year, month, day) < base:
                year += 1
            try:
                return dt.date(year, month, day)
            except ValueError:
                return None

    # "玩 3 天，5 号结束" 这类只给日号的情况：按最近的未来日期算
    m = re.search(r"(\d{1,2})\s*[日号]\s*(结束|回来|返回|返程|回程|收假|回家)", t)
    if m:
        day = int(m.group(1))
        for month_offset in (0, 1):
            year, month = base.year, base.month + month_offset
            if month > 12:
                year, month = year + 1, month - 12
            try:
                candidate = dt.date(year, month, day)
            except ValueError:
                continue
            if candidate >= base:
                return candidate
    return None


def _parse_iso(value: Any) -> Optional[dt.date]:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", value)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_days(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str):
        m = re.search(r"(\d+)\s*天", value)
        if m:
            return int(m.group(1))
    return None


def normalize_intent_dates(intent: Dict[str, Any], text: str = "",
                           reference: Optional[dt.date] = None) -> Dict[str, Any]:
    """把意图里的日期字段归一化（就地修改并返回）。

    规则（按优先级）：
      1. 用代码解析用户文本里的相对日期 → 覆盖模型给的 start_date（模型常受示例影响）
      2. start_date + days  → end_date = start + days - 1（自然日）
      3. start_date + end_date → days = end - start + 1
      4. end_date + days → start_date = end - days + 1
      5. 格式非法/无法解析 → 置 null（宁可为空，也不要给用户看错日期）
    """
    base = reference or today()
    days = _parse_days(intent.get("days"))
    start = _parse_iso(intent.get("start_date"))
    end = _parse_iso(intent.get("end_date"))

    parsed_start = parse_relative_start(text, base) if text else None
    parsed_end = parse_relative_end(text, base) if text else None

    if parsed_end is not None:
        # 文本里明确说了"X 月 X 日结束/回来" → 那是结束日，出发日由天数反推
        end = parsed_end
        if not days and start:
            days = (end - start).days + 1
        if days:
            start = end - dt.timedelta(days=days - 1)
        elif start and start > end:
            start = None
    elif parsed_start is not None:
        start = parsed_start

    if start and days:
        end = start + dt.timedelta(days=days - 1)
    elif start and end and end < start:
        end = start                       # 明显不合法：收敛为单日
    elif end and days and not start:
        start = end - dt.timedelta(days=days - 1)
    elif start and end:
        days = (end - start).days + 1

    if days is not None and days > 0:
        intent["days"] = f"{days}天"
    intent["start_date"] = start.isoformat() if start else None
    intent["end_date"] = end.isoformat() if end else None
    return intent
