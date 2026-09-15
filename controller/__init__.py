"""Controller 路由汇总"""

from fastapi import APIRouter

from controller.intent_routes import router as intent_router
from controller.summary_routes import router as summary_router
from controller.auth_routes import router as auth_router
from controller.chat_routes import router as chat_router
from controller.guide_routes import router as guide_router
from controller.share_routes import router as share_router
from controller.user_routes import router as user_router
from controller.dev_routes import router as dev_router

api_router = APIRouter()
api_router.include_router(intent_router)
api_router.include_router(summary_router)
api_router.include_router(auth_router)
api_router.include_router(chat_router)
api_router.include_router(guide_router)
api_router.include_router(share_router)
api_router.include_router(user_router)
api_router.include_router(dev_router)
