"""认证相关 API 路由"""

import uuid

from elastic_transport import ApiError, ConnectionError
from fastapi import APIRouter, HTTPException

from auth import hash_password, verify_password, create_access_token
from models.schemas import UserRegister, UserLogin, TokenResponse, UserResponse
from services.es_client import create_user, get_user_by_username

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", response_model=TokenResponse)
async def register(req: UserRegister):
    """用户注册"""
    username = req.username.strip()
    try:
        existing = get_user_by_username(username)
    except (ApiError, ConnectionError):
        raise HTTPException(status_code=503, detail="数据库连接失败，请稍后重试")
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    user_id = str(uuid.uuid4())
    nickname = (req.nickname or username).strip() or username
    password_hash = hash_password(req.password)

    try:
        user_doc = create_user(user_id, username, nickname, password_hash)
    except (ApiError, ConnectionError):
        raise HTTPException(status_code=503, detail="数据库连接失败，请稍后重试")

    token = create_access_token(user_id, username)
    return TokenResponse(
        access_token=token,
        user=UserResponse(
            user_id=user_id,
            username=username,
            nickname=nickname,
            created_at=user_doc["created_at"],
        ),
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: UserLogin):
    """用户登录"""
    username = req.username.strip()
    try:
        user = get_user_by_username(username)
    except (ApiError, ConnectionError):
        raise HTTPException(status_code=503, detail="数据库连接失败，请稍后重试")
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not verify_password(req.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(user["user_id"], user["username"])
    return TokenResponse(
        access_token=token,
        user=UserResponse(
            user_id=user["user_id"],
            username=user["username"],
            nickname=user.get("nickname", user["username"]),
            created_at=user.get("created_at", ""),
        ),
    )
