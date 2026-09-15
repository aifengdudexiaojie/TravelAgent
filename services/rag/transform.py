from typing import Any


# ============================================================
# 基础工具
# ============================================================

def _valid(value: Any) -> bool:
    """判断字段是否有有效内容。"""
    return value is not None and value != "" and value != [] and value != {}


def _text(value: Any) -> str:
    """将 list / dict / 普通值统一转换为文本。"""
    if not _valid(value):
        return ""

    if isinstance(value, list):
        return "、".join(
            str(v) for v in value
            if _valid(v)
        )

    if isinstance(value, dict):
        return "、".join(
            f"{k}：{v}"
            for k, v in value.items()
            if _valid(v)
        )

    return str(value)


def _join(parts: list[str], sep: str = "；") -> str:
    """过滤空字符串后拼接。"""
    return sep.join(
        str(x).strip()
        for x in parts
        if x and str(x).strip()
    )


def _field(value: Any, label: str) -> str:
    """统一生成 '标签：值'。"""
    if not _valid(value):
        return ""
    return f"{label}：{_text(value)}"


def _format_date(date_value: Any) -> str:
    """将日期统一格式化为 YYYY.M.D.。"""
    if not _valid(date_value):
        return ""
    s = str(date_value).strip()
    parts = s.split("-")
    if len(parts) == 3:
        try:
            return f"{parts[0]}.{int(parts[1])}.{int(parts[2])}"
        except ValueError:
            return s
    return s


def _days_text(total_days: Any) -> str:
    """统一处理 total_days 为 'X天' 形式。"""
    if not _valid(total_days):
        return ""
    s = str(total_days).replace("天", "").strip()
    if not s:
        return ""
    return f"{s}天"


def _int_days(total_days: Any) -> int | None:
    """尝试将 total_days 转为整数。"""
    if not _valid(total_days):
        return None
    s = str(total_days).replace("天", "").strip()
    try:
        return int(s)
    except ValueError:
        return None


def _clean_text(text: str) -> str:
    """将换行/多余空白替换为单个空格，保持语义连贯。"""
    if not text:
        return ""
    return " ".join(text.split())


# ============================================================
# 通用：扁平对象 / catalog item 格式化
# ============================================================

def _format_item(
    item: dict,
    field_mapping: dict[str, str],
    prefix: str = "",
) -> str:
    """
    根据 field_mapping 自动拼接对象。

    field_mapping:
        {
            "name": "名称",
            "location": "地点",
            ...
        }
    """
    if not isinstance(item, dict):
        return ""

    parts = [
        _field(item.get(key), label)
        for key, label in field_mapping.items()
    ]

    content = _join(parts)

    if not content:
        return ""

    return f"{prefix}{content}。"


def _format_items_list(
    items: Any,
    field_mapping: dict[str, str],
    prefix: str,
) -> list[str]:
    """
    通用 list[dict] 格式化，返回字符串数组（每条一个元素）。

    spots_catalog / food_catalog / extra_recommendations 等都可以复用。
    """
    if not isinstance(items, list):
        return []

    return [
        _format_item(item, field_mapping, prefix)
        for item in items
        if isinstance(item, dict)
    ]


def _format_items(
    items: Any,
    field_mapping: dict[str, str],
    prefix: str,
    item_sep: str = "\n",
) -> str:
    """
    通用 list[dict] 格式化（拼接成单字符串）。
    """
    return _join(
        _format_items_list(items, field_mapping, prefix),
        sep=item_sep,
    )


# ============================================================
# meta
# ============================================================

def _format_meta(meta: dict) -> str:
    """将 meta 拼接成自然语言摘要句。"""
    if not isinstance(meta, dict):
        return ""

    parts = []

    # destinations
    destinations = meta.get("destinations")
    dest_text = ""
    if _valid(destinations):
        if isinstance(destinations, list):
            dest_text = "、".join(
                str(d) for d in destinations if _valid(d)
            )
        else:
            dest_text = str(destinations)

    if dest_text:
        parts.append(f"该旅游攻略的目的地为{dest_text}")

    # date_range + total_days 合并成一句
    date_range = meta.get("date_range")
    start = end = ""
    if isinstance(date_range, dict):
        start = _format_date(date_range.get("start"))
        end = _format_date(date_range.get("end"))

    total_days = meta.get("total_days")
    days_text = _days_text(total_days)

    date_days_clause = ""
    if start and end:
        date_days_clause = f"从{start}到{end}"
    elif start:
        date_days_clause = f"从{start}开始"
    elif end:
        date_days_clause = f"到{end}结束"

    if days_text:
        date_days_clause += f"共{days_text}"

    if date_days_clause:
        parts.append(date_days_clause)

    # budget
    budget = meta.get("budget")
    if isinstance(budget, dict):
        estimated = budget.get("estimated")
        if _valid(estimated):
            parts.append(f"预估总花费为{estimated}")
        total = budget.get("total")
        if _valid(total):
            parts.append(f"总预算为{total}")

    # pace
    pace = meta.get("pace")
    if _valid(pace):
        parts.append(f"整体行程节奏为{pace}")

    return "，".join(parts) + "。" if parts else ""


# ============================================================
# daily_plan
# ============================================================

def _format_period(period_info: dict) -> str:
    """处理一天中的一个时段。"""

    if not isinstance(period_info, dict):
        return ""

    parts = []

    # content
    parts.append(
        _field(
            period_info.get("content"),
            "活动",
        )
    )

    # type
    parts.append(
        _field(
            period_info.get("type"),
            "类型",
        )
    )

    # detail
    detail = period_info.get("detail")

    if isinstance(detail, dict):

        detail_mapping = {
            "name": "名称",
            "summary": "说明",
            "duration": "时长",
            "cost": "消费",
        }

        for key, label in detail_mapping.items():
            parts.append(
                _field(
                    detail.get(key),
                    label,
                )
            )

        # 坐标一般不参与文本检索
        # 因此这里故意不拼 coordinates

    content = _join(parts)

    if not content:
        return ""

    period = period_info.get("period") or "该时段"

    return f"{period}：{content}。"


def _format_day(day_name: str, day_info: dict) -> str:
    """处理一天的行程。"""

    if not isinstance(day_info, dict):
        return ""

    parts = [day_name]

    # date
    if _valid(day_info.get("date")):
        parts.append(
            f"日期：{day_info['date']}"
        )

    # day_theme
    if _valid(day_info.get("day_theme")):
        parts.append(
            f"主题：{day_info['day_theme']}"
        )

    # periods
    periods = day_info.get("periods")

    if isinstance(periods, list):
        for period_info in periods:
            period_text = _format_period(period_info)

            if period_text:
                parts.append(period_text)

    # day_cost
    if _valid(day_info.get("day_cost")):
        parts.append(
            f"当天花费：{day_info['day_cost']}"
        )

    # tips
    if _valid(day_info.get("tips")):
        parts.append(
            f"特别提示：{_text(day_info['tips'])}"
        )

    return _join(parts, sep=" ") + "。"


def _format_daily_plan(daily_plan: dict) -> list[str]:
    """将 daily_plan 转换为每天一条字符串的数组。"""
    if not isinstance(daily_plan, dict):
        return []

    days = [
        _format_day(day_name, day_info)
        for day_name, day_info in daily_plan.items()
    ]

    return [d for d in days if d]


# ============================================================
# trade_off_summary
# ============================================================

def _format_trade_off_summary(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    mapping = {
        "total_spots_found": "候选景点总数",
        "spots_selected": "选中景点数",
        "spots_excluded": "排除景点数",
    }

    parts = [
        _field(data.get(key), label)
        for key, label in mapping.items()
    ]

    # exclusion_reasons 是动态 dict
    reasons = data.get("exclusion_reasons")

    if isinstance(reasons, dict):
        reason_text = _join(
            [
                f"{name}：{reason}"
                for name, reason in reasons.items()
                if _valid(name) and _valid(reason)
            ]
        )

        if reason_text:
            parts.append(
                f"排除原因：{reason_text}"
            )

    content = _join(parts)

    return (
        f"行程取舍信息：{content}。"
        if content
        else ""
    )


# ============================================================
# spots_catalog
# ============================================================

SPOTS_FIELDS = {
    "name": "名称",
    "type": "类型",
    "location": "地点",
    "summary": "简介",
    "highlights": "亮点",
    "precautions": "注意事项",
    "duration": "建议游览时长",
    "cost": "参考消费",
    "recommendation_count": "被推荐次数",
    "sources": "来源",
}


def _format_spots_catalog(data: list) -> str:
    return _format_items(
        data,
        SPOTS_FIELDS,
        prefix="景点信息：",
    )


# ============================================================
# food_catalog
# ============================================================

FOOD_FIELDS = {
    "name": "名称",
    "category": "类别",
    "location": "地点",
    "recommended_dishes": "推荐菜品",
    "summary": "简介",
    "avg_cost": "人均消费",
    "precautions": "注意事项",
    "sources": "来源",
}


def _format_food_catalog(data: list) -> str:
    return _format_items(
        data,
        FOOD_FIELDS,
        prefix="美食信息：",
    )


# ============================================================
# extra_recommendations
# ============================================================

EXTRA_FIELDS = {
    "name": "名称",
    "type": "类型",
    "location": "地点",
    "summary": "简介",
    "duration": "建议时长",
    "cost": "参考消费",
    "recommend_reason": "推荐理由",
    "sources": "来源",
}


def _format_extra_recommendations(data: list) -> str:
    return _format_items(
        data,
        EXTRA_FIELDS,
        prefix="额外推荐：",
    )


# ============================================================
# precautions_summary
# ============================================================

PRECAUTION_FIELDS = {
    "tickets": "门票",
    "transport": "交通",
    "timing": "时间",
    "cost": "消费",
    "other": "其他",
}


def _format_precautions_summary(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    parts = [
        _field(data.get(key), label)
        for key, label in PRECAUTION_FIELDS.items()
    ]

    content = _join(parts)

    return (
        f"旅游注意事项：{content}。"
        if content
        else ""
    )


# ============================================================
# budget_breakdown
# ============================================================

BUDGET_FIELDS = {
    "tickets": "门票费用",
    "food": "餐饮费用",
    "transport": "交通费用",
    "accommodation": "住宿费用",
    "other": "其他费用",
    "total": "总计",
    "remaining": "预算余额",
}


def _format_budget_breakdown(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    parts = [
        _field(data.get(key), label)
        for key, label in BUDGET_FIELDS.items()
    ]

    content = _join(parts)

    return (
        f"预算明细：{content}。"
        if content
        else ""
    )


# ============================================================
# keywords
# ============================================================

def _build_keywords(
    meta: dict,
    spots_catalog: list,
    food_catalog: list,
) -> list[str]:

    keywords = []

    # destinations
    destinations = meta.get("destinations", [])

    if isinstance(destinations, list):
        keywords.extend(
            str(x)
            for x in destinations
            if _valid(x)
        )
    elif _valid(destinations):
        keywords.append(str(destinations))

    # spots
    if isinstance(spots_catalog, list):
        keywords.extend(
            str(item["name"])
            for item in spots_catalog
            if isinstance(item, dict)
            and _valid(item.get("name"))
        )

    # food
    if isinstance(food_catalog, list):
        keywords.extend(
            str(item["name"])
            for item in food_catalog
            if isinstance(item, dict)
            and _valid(item.get("name"))
        )

    # 保持顺序去重
    return list(dict.fromkeys(keywords))


# ============================================================
# 主函数
# ============================================================

def es_info_transfer(guide: dict) -> dict:
    """
    将 ES 中的攻略信息转换为长串的文字用于存储到ES和Qdrant中。
    将完整旅游攻略 JSON 转换成：

    1. es_doc
       Elasticsearch 可直接 index 的 document

    2. vector_content
       可直接送入 embedding 模型的文本

    3. qdrant_payload
       Qdrant payload

    4. vector_content
       可直接送入 embedding 模型的文本数组（每个元素是一个语义 chunk）
    """

    if not isinstance(guide, dict):
        raise TypeError("guide 必须是 dict")

    meta = guide.get("meta") or {}

    # --------------------------------------------------------
    # 基础数据
    # --------------------------------------------------------

    destinations = meta.get("destinations", [])
    total_days = meta.get("total_days")

    spots_catalog = guide.get("spots_catalog") or []
    food_catalog = guide.get("food_catalog") or []

    # --------------------------------------------------------
    # 各模块转换
    # --------------------------------------------------------

    meta_text = _format_meta(meta)
    trade_off_text = _format_trade_off_summary(
        guide.get("trade_off_summary")
    )
    daily_plan_days = _format_daily_plan(
        guide.get("daily_plan")
    )
    daily_plan_text = _join(daily_plan_days, sep="\n")

    spots_texts = _format_items_list(
        spots_catalog, SPOTS_FIELDS, "景点信息："
    )
    spots_text = _join(spots_texts, sep="\n")

    food_texts = _format_items_list(
        food_catalog, FOOD_FIELDS, "美食信息："
    )
    food_text = _join(food_texts, sep="\n")

    extra_recommendations = guide.get("extra_recommendations") or []
    extra_texts = _format_items_list(
        extra_recommendations, EXTRA_FIELDS, "额外推荐："
    )
    extra_text = _join(extra_texts, sep="\n")

    precautions_text = _format_precautions_summary(
        guide.get("precautions_summary")
    )
    budget_text = _format_budget_breakdown(
        guide.get("budget_breakdown")
    )

    # --------------------------------------------------------
    # 清理换行，保持语义连贯
    # --------------------------------------------------------

    meta_text = _clean_text(meta_text)
    trade_off_text = _clean_text(trade_off_text)
    daily_plan_days = [_clean_text(d) for d in daily_plan_days]
    daily_plan_text = _clean_text(daily_plan_text)
    spots_texts = [_clean_text(s) for s in spots_texts]
    spots_text = _clean_text(spots_text)
    food_texts = [_clean_text(f) for f in food_texts]
    food_text = _clean_text(food_text)
    extra_texts = [_clean_text(e) for e in extra_texts]
    extra_text = _clean_text(extra_text)
    precautions_text = _clean_text(precautions_text)
    budget_text = _clean_text(budget_text)

    # --------------------------------------------------------
    # 核心检索文本（ES 用单字符串）
    # --------------------------------------------------------

    sections = [
        meta_text,
        trade_off_text,
        daily_plan_text,
        spots_text,
        food_text,
        extra_text,
        precautions_text,
        budget_text,
    ]

    search_content = _clean_text(_join(sections, sep=" "))

    # --------------------------------------------------------
    # 向量内容（Qdrant 用文本数组，每个元素即一个 chunk）
    # --------------------------------------------------------

    vector_content: list[str] = []

    if meta_text:
        vector_content.append(meta_text)
    if trade_off_text:
        vector_content.append(trade_off_text)
    vector_content.extend(daily_plan_days)
    vector_content.extend(spots_texts)
    vector_content.extend(food_texts)
    vector_content.extend(extra_texts)
    if precautions_text:
        vector_content.append(precautions_text)
    if budget_text:
        vector_content.append(budget_text)

    # --------------------------------------------------------
    # keywords
    # --------------------------------------------------------

    keywords = _build_keywords(
        meta,
        spots_catalog,
        food_catalog,
    )

    # --------------------------------------------------------
    # 基础字段
    # --------------------------------------------------------

    guide_id = (
        guide.get("guide_id")
        or guide.get("id")
    )

    user_id = guide.get("user_id")

    title = (
        guide.get("title")
        or meta.get("title")
        or ""
    )

    # destination：
    # 单目的地 → 直接字符串
    # 多目的地 → 拼接字符串
    if isinstance(destinations, list):

        if len(destinations) == 1:
            destination = destinations[0]
        else:
            destination = _text(destinations)

    else:
        destination = destinations

    # 将 total_days 统一为整数，便于 ES 索引
    days = _int_days(total_days)

    # --------------------------------------------------------
    # Elasticsearch document
    # --------------------------------------------------------

    es_doc = {
        "guide_id": guide_id,
        "user_id": user_id,

        "is_public": guide.get(
            "is_public",
            False,
        ),

        "title": title,

        "destination": destination,

        # 自然语言 meta 摘要
        "summary": meta_text,

        "days": days,

        "keywords": keywords,

        # 完整拼接文本，用于全文 BM25 检索
        "search_content": search_content,

        # 结构化二级字段：数组形式，便于精准/片段检索
        "daily_plan": daily_plan_days,
        "daily_plan_text": daily_plan_text,

        "spots_catalog": spots_texts,
        "food_catalog": food_texts,
        "extra_recommendations": extra_texts,

        "precautions_summary": precautions_text,
        "budget_breakdown": budget_text,
        "trade_off_summary": trade_off_text,

        "updated_at": (
            meta.get("generated_at")
            or guide.get("updated_at")
        ),
    }

    # 删除 None
    es_doc = {
        key: value
        for key, value in es_doc.items()
        if value is not None
    }

    # --------------------------------------------------------
    # Qdrant payload
    # --------------------------------------------------------

    qdrant_payload = {
        "guide_id": guide_id,
        "user_id": user_id,

        "is_public": guide.get(
            "is_public",
            False,
        ),

        "title": title,

        "destination": destinations,

        "days": days,

        "generated_at": meta.get(
            "generated_at"
        ),
    }

    qdrant_payload = {
        key: value
        for key, value in qdrant_payload.items()
        if value is not None
    }

    # --------------------------------------------------------
    # 返回
    # --------------------------------------------------------

    return {
        "es_doc": es_doc,
        "vector_content": vector_content,
        "qdrant_payload": qdrant_payload,

        # 兼容 services/guide_ingest.build_search_doc 的扁平字段
        "overview": meta_text,
        "daily_plan": daily_plan_text,
        "spots_catalog": spots_text,
        "food_catalog": food_text,
        "extra_recommendations": extra_text,
        "precautions_summary": precautions_text,
        "budget_breakdown": budget_text,
    }