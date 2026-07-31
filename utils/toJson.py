"""
LLM 返回文本 → JSON 工具

功能：
  1. to_json()     — 去掉 ```json ``` 标记，解析为 JSON（含截断修复）
  2. clean_intent() — 从 AI 返回中提取 IntentResponse 所需的字段
"""

import json
import re


def to_json(text: str) -> dict:
    """
    将 LLM 返回的文本转为 JSON。

    1. 去掉 ```json ... ``` 或 ``` ... ``` 包裹
    2. 解析为 JSON（自动修复截断和语法错误）
    """
    if not text or not text.strip():
        raise json.JSONDecodeError("Empty response", text, 0)

    text = text.strip()

    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    return _parse_json(text)


def _parse_json(text: str) -> dict:
    """尝试多种方式解析 JSON，处理 LLM 常见的语法错误和截断"""

    # 1. 标准解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    cleaned = text

    # 2. 去掉末尾孤立冒号和不完整 key: value
    #    如 "summary": "xxx"\n :  → 去掉末尾的 " :"
    lines = cleaned.rstrip().split('\n')
    # 从最后一行往上看，如果最后一行是孤立的 " :" 或只有冒号，去掉
    while lines and re.match(r'^\s*:\s*$', lines[-1]):
        lines.pop()
    # 如果最后一行包含冒号但明显不完整（没有值）
    if lines and re.search(r':\s*"[^"]*$', lines[-1]):
        # 尝试补全引号
        lines[-1] += '"'
    cleaned = '\n'.join(lines)

    # 3. 去掉尾部的逗号，补全未闭合引号和括号
    cleaned = re.sub(r',\s*}', '}', cleaned)
    cleaned = re.sub(r',\s*]', ']', cleaned)

    if cleaned.count('"') % 2 != 0:
        cleaned += '"'

    opens = cleaned.count('{') + cleaned.count('[')
    closes = cleaned.count('}') + cleaned.count(']')
    for _ in range(opens - closes):
        cleaned += '}' if not cleaned.rstrip().endswith(']') else ']'

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 4. 尝试提取最外层的 {}
    match = re.search(r'(\{.*\})', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 5. 单引号替换为双引号
    try:
        return json.loads(cleaned.replace("'", '"'))
    except json.JSONDecodeError:
        pass

    raise json.JSONDecodeError(f"无法解析 JSON: {text[:200]}", text, 0)


def clean_intent(data: dict) -> dict:
    """
    从 AI 返回的完整数据中，提取 IntentResponse 所需的字段。

    AI 可能返回额外字段（raw_query、intent_confidence、needs_clarification 等），
    此函数只保留 IntentResponse 校验需要的字段，多余的自动丢弃。

    支持两种输入格式：
      - 有 intent 包裹：{"intent": {"location": [...], "days": "3天", ...}, ...}
      - 无 intent 包裹：{"location": [...], "days": "3天", ...}
    """
    source = data.get("intent", data)

    fields = [
        ("locations", ["location"], ["未知"]),
        ("days", ["time", "duration"], "2天"),
        ("start_date", ["start_day", "startDay"], None),
        ("end_date", ["end_day", "endDay"], None),
        ("people_count", ["peopleCount", "people_num"], None),
        ("pace", ["pace", "state", "rhythm"], "正常"),
        ("budget_level", ["budgetLevel"], "正常"),
        ("budget_amount", ["budget_amount", "budget"], None),
        ("budget_amount_per_person", ["budget_amount_per_person", "budget_every_person"], None),
        ("others", ["others", "notes"], None),
    ]

    result = {}
    for target_key, source_keys, default in fields:
        value = None
        if source.get(target_key) is not None:
            value = source[target_key]
        else:
            for key in source_keys:
                v = source.get(key)
                if v is not None:
                    value = v
                    break

        if value is None:
            value = default

        if target_key == "locations" and isinstance(value, str):
            value = [value]
        if target_key == "days" and isinstance(value, (int, float)):
            value = f"{int(value)}天"

        result[target_key] = value

    return result
