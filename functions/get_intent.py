"""意图识别：接收用户自然语言 → 输出结构化 JSON（非流式，一次返回）"""

import json
import time
from typing import AsyncIterator
from agents.tips_agent import GeneralAgent
from utils.toJson import to_json, clean_intent


# ================================================================
# 流式版本（保留兼容，后续接入真实 Agent 后使用）
# ================================================================


async def get_user_intent_stream(input_text: str) -> AsyncIterator[str]:
    """（保留）流式识别 - 后续接入 agents/tips_agent 时启用"""


    intent_agent = GeneralAgent("deepseek", "Intent")
    messages = [{"role": "user", "content": input_text}]

    full_response = ""

    try:
        async for chunk in intent_agent.chat_stream(messages):
            full_response += chunk
            yield _sse_event("chunk", chunk)

        try:
            parsed = json.loads(full_response)
            yield _sse_event("done", parsed)
        except json.JSONDecodeError:
            yield _sse_event("done", {"raw": full_response})

    except Exception as e:
        yield _sse_event("error", {"message": str(e)})


# ================================================================
# 非流式版本（当前使用 - 一次输出完整结果）
# ================================================================


async def get_user_intent(input_text: str) -> dict:
    """
    正常输出
    """
    try:
        intent_agent = GeneralAgent("deepseek", "Intent")
        messages = [{"role": "user", "content": input_text}]

        json_response = await intent_agent.chat(messages)
        if not json_response or not json_response.strip():
            return _fallback_intent(input_text)

        try:
            tojson = json.loads(json_response)
            # 将查询内容交给保存 并进行数据库存档

            parsed = clean_intent(tojson)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            # AI 返回了非 JSON 文本，兜底返回结构化的占位数据
            return _fallback_intent(input_text, raw=json_response)

    except ImportError:
        # agents.tips_agent 尚未实现，返回 Mock 数据
        return _fallback_intent(input_text)
    except Exception as e:
        return {"error": str(e), **_fallback_intent(input_text)}


def _fallback_intent(input_text: str, raw: str | None = None) -> dict:
    """AI 不可用时的兜底数据"""
    import re
    text = input_text.lower()

    # 提取目的地
    locations = []
    for city in ["成都", "宜兴", "宣城", "北京", "上海", "广州", "深圳", "杭州", "南京", "重庆"]:
        if city in text:
            locations.append(city)

    # 提取预算
    budget = 3000
    match = re.search(r'(\d+)\s*元', text)
    if match:
        budget = float(match.group(1))

    # 提取天数
    day_match = re.search(r'(\d+)\s*天', text)
    time_str = f"{day_match.group(1)}天" if day_match else "3天"

    result = {
        "time": time_str,
        "start_day": None,
        "end_day": None,
        "locations": locations or ["未知"],
        "state": "relaxed",
        "budget": budget,
        "others": None,
    }
    if raw:
        result["_raw"] = raw
    return result



def _extract_budget(text: str, default: float = 3000) -> float:
    """从文本中提取预算"""
    import re
    match = re.search(r'(\d+)\s*元', text)
    return float(match.group(1)) if match else default


def _sse_event(event_type: str, data) -> str:
    """构造 SSE 格式事件字符串"""
    payload = json.dumps({"type": event_type, "content": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"
