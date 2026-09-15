"""意图识别相关 API 路由 - 非流式版本"""
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from functions.get_intent import get_user_intent
from utils.redis_storage import RedisMemory
from utils.toJson import clean_intent


def get_redis():
    return RedisMemory()


router = APIRouter(prefix="/api/intent", tags=["意图识别"])


class IntentRequest(BaseModel):
    query: str


class IntentContent(BaseModel):
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


class RecognizeResponse(BaseModel):
    task_id: int
    intent_content: IntentContent


@router.post("/recognize", response_model=RecognizeResponse)
async def recognize_intent(req: IntentRequest, redis: RedisMemory = Depends(get_redis)):
    """
    阶段1：意图识别
    输入用户需求 → 生成 task_id + 识别意图 → 返回给前端展示确认
    """
    task_id, intent = await get_user_intent(req.query, redis)
    return RecognizeResponse(task_id=task_id, intent_content=IntentContent(**intent))
