"""旅游分享 API 路由"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user
from models.schemas import GuideRate, GuidePublish
from services.guide_service import rate_guide, publish_guide, unpublish_guide

router = APIRouter(prefix="/api/share", tags=["旅游分享"])


@router.post("/{guide_id}/rate")
async def rate_my_guide(
    guide_id: str,
    req: GuideRate,
    current_user: dict = Depends(get_current_user),
):
    """对攻略进行评价（公开前必须评价）"""
    success = rate_guide(guide_id, current_user["user_id"], req.rating, req.rating_text)
    if not success:
        raise HTTPException(status_code=400, detail="评价失败：攻略不存在或非本人攻略")
    return {"message": "评价成功"}


@router.post("/{guide_id}/publish")
async def publish_my_guide(
    guide_id: str,
    current_user: dict = Depends(get_current_user),
):
    """公开攻略（需已评价）"""
    from services.es_client import get_guide
    guide = get_guide(guide_id)
    if not guide:
        raise HTTPException(status_code=404, detail="攻略不存在")
    if guide.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="只能公开自己的攻略")
    if guide.get("rating") is None:
        raise HTTPException(status_code=400, detail="请先对攻略进行评价后再公开")

    success = publish_guide(guide_id, current_user["user_id"])
    if not success:
        raise HTTPException(status_code=500, detail="公开失败")
    return {"message": "攻略已公开"}


@router.post("/{guide_id}/unpublish")
async def unpublish_my_guide(
    guide_id: str,
    current_user: dict = Depends(get_current_user),
):
    """取消公开"""
    success = unpublish_guide(guide_id, current_user["user_id"])
    if not success:
        raise HTTPException(status_code=400, detail="操作失败")
    return {"message": "已取消公开"}


@router.get("/public")
async def list_public_guides(
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = Query(None, description="按标题/目的地/摘要/关键词模糊搜索"),
):
    """浏览他人公开的旅游攻略（支持关键字搜索）"""
    from services.es_client import list_public_guides
    result = list_public_guides(page=page, page_size=page_size, keyword=keyword)
    # 解析 content
    for item in result.get("items", []):
        if isinstance(item.get("content"), str):
            try:
                item["content"] = json.loads(item["content"])
            except (json.JSONDecodeError, TypeError):
                pass
    return result


@router.get("/public/{guide_id}")
async def get_public_guide(guide_id: str):
    """查看公开攻略详情"""
    from services.es_client import get_guide
    guide = get_guide(guide_id)
    if not guide:
        raise HTTPException(status_code=404, detail="攻略不存在")
    if not guide.get("is_public"):
        raise HTTPException(status_code=403, detail="该攻略未公开")
    if isinstance(guide.get("content"), str):
        try:
            guide["content"] = json.loads(guide["content"])
        except (json.JSONDecodeError, TypeError):
            pass
    return guide
