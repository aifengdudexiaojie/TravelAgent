"""攻略服务 - 生成、存储、向量化"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from services.es_client import save_guide, get_guide, update_guide, list_user_guides
from services.embedding import embed_text

logger = logging.getLogger(__name__)


def generate_summary_from_content(content: dict) -> str:
    """从攻略内容生成文字摘要（用于向量化存储）"""
    parts = []

    destination = content.get("destination", "")
    days = content.get("days", "")
    if destination:
        parts.append(f"{destination}{'%s天' % days if days else ''}旅行攻略")

    # 提取行程关键信息
    itinerary = content.get("itinerary", content.get("daily_plan", []))
    if isinstance(itinerary, list):
        locations = set()
        for day in itinerary:
            if isinstance(day, dict):
                for activity in day.get("activities", day.get("spots", [])):
                    if isinstance(activity, dict):
                        name = activity.get("name", activity.get("place", ""))
                        if name:
                            locations.add(name)
                    elif isinstance(activity, str):
                        locations.add(activity)
        if locations:
            parts.append(f"景点: {', '.join(list(locations)[:10])}")

    # 预算
    budget = content.get("budget", content.get("total_budget", ""))
    if budget:
        parts.append(f"预算: {budget}")

    # 美食
    food = content.get("food_recommendations", content.get("food", []))
    if isinstance(food, list) and food:
        food_names = [f.get("name", f) if isinstance(f, dict) else str(f) for f in food[:5]]
        parts.append(f"美食推荐: {', '.join(food_names)}")

    return "。".join(parts) if parts else "旅行攻略"


def extract_keywords(content: dict) -> List[str]:
    """从攻略内容提取关键词（存入元数据方便搜索）"""
    keywords = []

    destination = content.get("destination", "")
    if destination:
        keywords.append(destination)

    # 提取地点名
    itinerary = content.get("itinerary", content.get("daily_plan", []))
    if isinstance(itinerary, list):
        for day in itinerary:
            if isinstance(day, dict):
                for activity in day.get("activities", day.get("spots", [])):
                    if isinstance(activity, dict):
                        name = activity.get("name", activity.get("place", ""))
                        if name and name not in keywords:
                            keywords.append(name)

    # 标签
    tags = content.get("tags", content.get("keywords", []))
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag not in keywords:
                keywords.append(tag)

    return keywords[:20]


def create_guide_record(
    user_id: str,
    username: str,
    nickname: str,
    title: str,
    content: dict,
    destination: str,
    days: Optional[int] = None,
    summary: Optional[str] = None,
) -> dict:
    """创建攻略记录并存入 ES（含向量化）"""
    guide_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if not summary:
        summary = generate_summary_from_content(content)

    keywords = extract_keywords(content)

    # 向量化摘要
    embedding = None
    try:
        embedding = embed_text(summary)
    except Exception as e:
        logger.warning(f"攻略向量化失败: {e}")

    doc = {
        "guide_id": guide_id,
        "user_id": user_id,
        "username": username,
        "nickname": nickname,
        "title": title,
        "destination": destination,
        "days": days,
        "summary": summary,
        "content": json.dumps(content, ensure_ascii=False),
        "content_json": json.dumps(content, ensure_ascii=False),
        "keywords": keywords,
        "rating": None,
        "rating_text": None,
        "is_public": False,
        "created_at": now,
        "updated_at": now,
    }

    if embedding:
        doc["embedding"] = embedding

    saved_id = save_guide(doc)
    doc["guide_id"] = saved_id
    return doc


def rate_guide(guide_id: str, user_id: str, rating: int, rating_text: str) -> bool:
    """用户评价攻略"""
    guide = get_guide(guide_id)
    if not guide:
        return False
    if guide.get("user_id") != user_id:
        return False

    return update_guide(guide_id, {
        "rating": rating,
        "rating_text": rating_text,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def publish_guide(guide_id: str, user_id: str) -> bool:
    """公开攻略（需已评价）"""
    guide = get_guide(guide_id)
    if not guide:
        return False
    if guide.get("user_id") != user_id:
        return False
    if guide.get("rating") is None:
        return False

    return update_guide(guide_id, {
        "is_public": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def unpublish_guide(guide_id: str, user_id: str) -> bool:
    """取消公开"""
    guide = get_guide(guide_id)
    if not guide or guide.get("user_id") != user_id:
        return False
    return update_guide(guide_id, {
        "is_public": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
