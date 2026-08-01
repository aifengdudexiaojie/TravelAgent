"""意图识别相关 API 路由 - 非流式版本"""
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel
from fastapi import Depends
from functions.get_intent import get_user_intent
from utils.redis_storage import RedisMemory
from utils.toJson import clean_intent


def get_redis():
    return RedisMemory()

router = APIRouter(prefix="/api/intent", tags=["意图识别"])


class IntentRequest(BaseModel):
    query: str


class IntentResponse(BaseModel):
    locations: List[str]
    days: str | None
    start_date: str | None
    end_date: str | None
    people_count: int | None
    pace: str
    budget_level: str
    budget_amount: int | None
    budget_amount_per_person: int | None
    others: str | None


@router.post("/recognize", response_model=IntentResponse)
async def recognize_intent(req: IntentRequest, redis: RedisMemory = Depends(get_redis)):
    """
    识别用户旅行意图（非流式，一次输出完整结果）

    AI 返回 → clean_intent 去多余字段 → IntentResponse 校验 → 返回前端
    """
    cleaned = await get_user_intent(req.query, redis)
    return IntentResponse(**cleaned)
