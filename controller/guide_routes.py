"""旅游攻略 API 路由"""

import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from auth import get_current_user
from models.schemas import GuideCreate, GuideResponse
from services.guide_service import create_guide_record

router = APIRouter(prefix="/api/guides", tags=["旅游攻略"])


@router.post("/save")
async def save_travel_guide(
    req: GuideCreate,
    current_user: dict = Depends(get_current_user),
):
    """保存生成的旅游攻略到 ES"""
    doc = create_guide_record(
        user_id=current_user["user_id"],
        username=current_user["username"],
        nickname=current_user.get("nickname", current_user["username"]),
        title=req.title,
        content=req.content,
        destination=req.destination,
        days=req.days,
        summary=req.summary,
    )
    return {"guide_id": doc["guide_id"], "message": "攻略已保存"}


@router.get("/my")
async def my_guides(
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = Query(None, description="按标题/目的地/摘要/关键词模糊搜索"),
    current_user: dict = Depends(get_current_user),
):
    """获取我的攻略列表（支持关键字搜索）"""
    from services.es_client import list_user_guides
    result = list_user_guides(current_user["user_id"], page=page, page_size=page_size,
                             keyword=keyword)
    return result


@router.get("/{guide_id}")
async def get_guide_detail(
    guide_id: str,
    current_user: dict = Depends(get_current_user),
):
    """获取攻略详情（本人攻略，或他人已公开的攻略）。

    返回里带 `is_owner`：前端据此决定是否显示「评价 / 公开 / 取消公开」等管理操作。
    """
    from services.es_client import get_guide
    guide = get_guide(guide_id)
    if not guide:
        raise HTTPException(status_code=404, detail="攻略不存在")

    is_owner = guide.get("user_id") == current_user["user_id"]
    if not is_owner and not guide.get("is_public"):
        raise HTTPException(status_code=403, detail="无权查看该攻略（未公开且非本人攻略）")

    # 解析 content JSON
    if isinstance(guide.get("content"), str):
        try:
            guide["content"] = json.loads(guide["content"])
        except (json.JSONDecodeError, TypeError):
            pass
    guide["is_owner"] = is_owner
    return guide
