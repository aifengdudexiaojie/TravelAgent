"""小红书 MCP 路由：二维码登录 / 状态 / 实例启停 / cookies 导入
================================================================================

前端「生成旅游攻略」页顶部显示登录状态并据此决定能否开始规划：

    GET  /api/xhs/status          当前用户状态（已登录？实例在跑？平台？为什么起不来？）
    GET  /api/xhs/qrcode          MCP 的静态二维码（备用方案）
    POST /api/xhs/login/start     **网页扫码登录（推荐）**：服务器自己开浏览器，返回实时二维码
    GET  /api/xhs/login/probe     探测登录进度：每次返回**最新**二维码 / 二次验证码 / 已登录
    POST /api/xhs/login/refresh   换一张新二维码（安全：登录会话就是我们自己的浏览器）
    POST /api/xhs/login/stop      关闭登录浏览器
    POST /api/xhs/clear           退出登录（换号用；会删掉该用户的 cookies）
    POST /api/xhs/cookies         导入 cookies.json（备用方案）
    POST /api/xhs/login/desktop   桌面环境备用：拉起登录程序弹浏览器扫码
    POST /api/xhs/mcp/start       启动该用户的 MCP 实例（登录前也会自动启动）
    POST /api/xhs/mcp/stop        停止该用户的 MCP 实例
    POST /api/xhs/logout          停止实例（不删登录态）
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from services import xhs_manager
from services import xhs_login_browser as live_login

logger = logging.getLogger("controller.xhs")

router = APIRouter(prefix="/api/xhs", tags=["小红书 MCP"])


class CookiesPayload(BaseModel):
    """cookies.json 的文本内容（前端 FileReader 读出来直接传，避免 multipart 依赖）。"""
    cookies: str


@router.get("/status")
async def xhs_status(current_user: dict = Depends(get_current_user)):
    """当前用户的小红书登录/实例状态（前端轮询这个接口点亮绿灯）。"""
    import asyncio

    return await asyncio.to_thread(xhs_manager.status_for, current_user["user_id"])


# ================================================================
# 网页扫码登录（自建浏览器，实时二维码；推荐路径）
# ================================================================
@router.post("/login/start")
async def xhs_live_login_start(current_user: dict = Depends(get_current_user)):
    """开始扫码登录：服务器自己开一个浏览器，把**当前**二维码返回给前端。"""
    import asyncio

    return await asyncio.to_thread(live_login.start, current_user["user_id"])


@router.get("/login/probe")
async def xhs_live_login_probe(current_user: dict = Depends(get_current_user)):
    """探测登录进度。

    每次都用**最新**的二维码（小红书自己的码约 1~2 分钟就换一次），
    因此不会出现"MCP 只截一张图、用户扫到过期码"的问题；
    出现二次设备安全验证时会返回那一张码（state=verify）。
    """
    import asyncio

    return await asyncio.to_thread(live_login.probe, current_user["user_id"])


@router.post("/login/refresh")
async def xhs_live_login_refresh(current_user: dict = Depends(get_current_user)):
    """换一张新二维码（重新打开登录页）。

    与 MCP 的 `get_login_qrcode` 不同，这里换码是安全的：登录会话就是我们自己
    这个浏览器，不存在"再取一次就把上一个会话关掉"的情况。
    """
    import asyncio

    return await asyncio.to_thread(live_login.refresh, current_user["user_id"])


@router.post("/login/stop")
async def xhs_live_login_stop(current_user: dict = Depends(get_current_user)):
    """关闭登录浏览器（用户关掉弹窗时调用，释放内存）。"""
    import asyncio

    return await asyncio.to_thread(live_login.stop, current_user["user_id"])


@router.get("/login/available")
async def xhs_live_login_available(current_user: dict = Depends(get_current_user)):
    """网页扫码登录是否可用（不可用时前端退回 MCP 的静态二维码方案）。"""
    import asyncio

    info = await asyncio.to_thread(live_login.availability)
    info["sessions"] = await asyncio.to_thread(live_login.active_sessions)
    return info


@router.get("/qrcode")
async def xhs_qrcode(refresh: int = 0,
                     current_user: dict = Depends(get_current_user)):
    """取登录二维码（Base64 PNG）—— MCP 方案，作为**备用**。

    ⚠️ `refresh=1` 才真的向 MCP 重新取码。默认（0）返回**缓存**的二维码，因为
    上游 issue #799：重复调用 `get_login_qrcode` 会新建浏览器、取消旧会话，
    用户刚在手机上确认的登录/设备验证上下文会被顶掉（表现为"扫码后没反应"）。
    """
    import asyncio

    return await asyncio.to_thread(xhs_manager.login_qrcode,
                                   current_user["user_id"], bool(refresh))


@router.post("/clear")
async def xhs_clear(current_user: dict = Depends(get_current_user)):
    """退出登录（换号/重新扫码前调用）：删该用户的 cookies 并停实例。"""
    import asyncio

    return await asyncio.to_thread(xhs_manager.clear_login, current_user["user_id"])


@router.post("/cookies")
async def xhs_import_cookies(req: CookiesPayload,
                             current_user: dict = Depends(get_current_user)):
    """导入 cookies.json：无桌面服务器（云端 Linux）上替代扫码登录的方式。

    在有桌面的机器上登录一次拿到 cookies.json，把**文件内容**POST 到这里，
    服务端会写入该用户自己的工作目录，并在实例运行中时自动重启使其生效。
    """
    import asyncio

    return await asyncio.to_thread(xhs_manager.import_cookies,
                                   current_user["user_id"], req.cookies)


@router.post("/login/desktop")
async def xhs_login(current_user: dict = Depends(get_current_user)):
    """拉起小红书登录程序（扫码登录）——**仅桌面环境**，服务器请用 /login/start。"""
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
