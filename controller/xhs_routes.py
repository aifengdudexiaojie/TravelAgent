"""小红书 MCP 路由：登录 / 状态 / 实例启停
================================================================================

前端「生成旅游攻略」页顶部会显示登录状态，并据此决定能否开始规划：

    GET  /api/xhs/status          当前用户的小红书状态（已登录？实例在跑？模式？）
    POST /api/xhs/login           拉起登录程序（用户扫码登录自己的小红书）
    POST /api/xhs/mcp/start       启动该用户的 MCP 实例（登录前也会自动启动）
    POST /api/xhs/mcp/stop        停止该用户的 MCP 实例
    POST /api/xhs/logout          停止实例（登录态由 cookies.json 保存，重登会覆盖）
"""

import logging

from fastapi import APIRouter, Depends

from auth import get_current_user
from services import xhs_manager

logger = logging.getLogger("controller.xhs")

router = APIRouter(prefix="/api/xhs", tags=["小红书 MCP"])


@router.get("/status")
async def xhs_status(current_user: dict = Depends(get_current_user)):
    """当前用户的小红书登录/实例状态（前端轮询这个接口点亮绿灯）。"""
    import asyncio

    return await asyncio.to_thread(xhs_manager.status_for, current_user["user_id"])


@router.post("/login")
async def xhs_login(current_user: dict = Depends(get_current_user)):
    """拉起小红书登录程序（扫码登录）——会为该用户准备独立工作目录与实例。"""
    import asyncio

    user_id = current_user["user_id"]

    def _flow():
        started = xhs_manager.start_mcp(user_id)
        login = xhs_manager.start_login(user_id)
        status = xhs_manager.status_for(user_id)
        return {
            "mcp": started,
            "login": login,
            "status": status,
            "message": login.get("message") or started.get("message") or "",
        }

    return await asyncio.to_thread(_flow)


@router.post("/mcp/start")
async def xhs_start_mcp(current_user: dict = Depends(get_current_user)):
    """启动当前用户的 MCP 实例（幂等）。"""
    import asyncio

    return await asyncio.to_thread(xhs_manager.start_mcp, current_user["user_id"])


@router.post("/mcp/stop")
async def xhs_stop_mcp(current_user: dict = Depends(get_current_user)):
    """停止当前用户的 MCP 实例（释放内存/端口）。"""
    import asyncio

    stopped = await asyncio.to_thread(xhs_manager.stop_mcp, current_user["user_id"])
    return {"stopped": stopped}


@router.post("/logout")
async def xhs_logout(current_user: dict = Depends(get_current_user)):
    """退出：停掉实例即可；cookies.json 留在该用户自己的目录里，重新登录会覆盖。"""
    import asyncio

    await asyncio.to_thread(xhs_manager.stop_mcp, current_user["user_id"])
    return {"ok": True, "message": "已停止该用户的小红书实例（登录态仍保存在其工作目录，重新登录可覆盖）"}
