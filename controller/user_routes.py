"""用户中心 API 路由"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from services.es_client import get_user_by_id, list_user_guides

router = APIRouter(prefix="/api/user", tags=["用户中心"])


@router.get("/profile")
async def get_profile(current_user: dict = Depends(get_current_user)):
    """获取用户信息"""
    user = get_user_by_id(current_user["user_id"])
    if not user:
        return {"user_id": current_user["user_id"], "username": current_user["username"]}
    return {
        "user_id": user.get("user_id"),
        "username": user.get("username"),
        "nickname": user.get("nickname"),
        "created_at": user.get("created_at"),
    }


@router.get("/guides")
async def my_guides(
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = Query(None, description="按标题/目的地/摘要/关键词模糊搜索"),
    current_user: dict = Depends(get_current_user),
):
    """我的攻略列表（含草稿、已评价、已公开；支持关键字搜索）"""
    result = list_user_guides(current_user["user_id"], page=page, page_size=page_size,
                              keyword=keyword)
    return result


@router.get("/stats")
async def my_stats(current_user: dict = Depends(get_current_user)):
    """我的统计数据"""
    result = list_user_guides(current_user["user_id"], page=1, page_size=1000)
    items = result.get("items", [])
    total = len(items)
    public_count = sum(1 for g in items if g.get("is_public"))
    rated_count = sum(1 for g in items if g.get("rating") is not None)
    return {
        "total_guides": total,
        "public_guides": public_count,
        "rated_guides": rated_count,
    }
