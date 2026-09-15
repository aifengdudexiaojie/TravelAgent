"""用户、攻略、聊天相关 Pydantic 模型"""

from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime
from enum import Enum


# ================================================================
# 用户相关
# ================================================================

class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=64)
    nickname: Optional[str] = None


class UserLogin(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    user_id: str
    username: str
    nickname: str
    created_at: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


# ================================================================
# 旅游攻略相关
# ================================================================

class GuideStatus(str, Enum):
    DRAFT = "draft"
    RATED = "rated"
    PUBLIC = "public"


class GuideCreate(BaseModel):
    """创建攻略（从现有规划结果保存）"""
    title: str
    content: dict
    destination: str
    days: Optional[int] = None
    summary: Optional[str] = None


class GuideRate(BaseModel):
    """用户对攻略进行评价"""
    rating: int = Field(..., ge=1, le=5)
    rating_text: str = Field(..., min_length=10, max_length=500)


class GuidePublish(BaseModel):
    """公开攻略（需已评价）"""
    pass


class GuideResponse(BaseModel):
    guide_id: str
    user_id: str
    username: str
    nickname: str
    title: str
    destination: str
    days: Optional[int]
    summary: Optional[str]
    content: dict
    rating: Optional[int]
    rating_text: Optional[str]
    is_public: bool
    created_at: str
    updated_at: str


class GuideListItem(BaseModel):
    guide_id: str
    username: str
    nickname: str
    title: str
    destination: str
    days: Optional[int]
    summary: Optional[str]
    rating: Optional[int]
    rating_text: Optional[str]
    created_at: str


# ================================================================
# 聊天相关
# ================================================================

class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    history: Optional[List[ChatMessage]] = []
    """
    聊天模式（三选一）：
      normal   普通：不检索、不做意图判定，直接由模型回答
      history  历史模式：先做关键词 + LLM 意图判定，判定需要时**只检索自己的攻略**
      public   公开模式：不做意图判定，**检索"自己的 + 他人已公开的"攻略**再回答

    history_mode 为兼容字段：chat_mode 未传（None）时 history_mode=True 等价于 chat_mode="history"。
    """
    chat_mode: Optional[Literal["normal", "history", "public"]] = None
    history_mode: bool = False


class ChatResponse(BaseModel):
    reply: str
    session_id: Optional[str] = None
    used_rag: bool
    chat_mode: str = "normal"
    history_mode: bool = False
    rag_decision: Optional[dict] = None
    related_guides: List[dict] = []
    trip_brief: Optional[dict] = None
    next_questions: List[str] = []


class SessionResponse(BaseModel):
    session_id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    message_count: int = 0


class TripBriefResponse(BaseModel):
    brief: dict
    version: int = 0
    updated_at: Optional[str] = None


# ================================================================
# 通用
# ================================================================

class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
