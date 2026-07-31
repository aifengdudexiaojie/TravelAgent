"""Controller 层：将 functions 层的业务逻辑暴露为 RESTful API"""

from fastapi import APIRouter

from .intent_routes import router as intent_router

# 主路由：汇聚所有子路由
api_router = APIRouter()
api_router.include_router(intent_router)
