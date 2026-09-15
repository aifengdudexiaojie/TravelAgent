"""小红书 MCP 并发控制 + 登录状态管理"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 最大同时使用小红书 MCP 的用户数
MAX_CONCURRENT_USERS = int(__import__("os").getenv("XHS_MAX_CONCURRENT", "3"))

_semaphore: Optional[asyncio.Semaphore] = None
_active_users: set = set()
_lock = asyncio.Lock()


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_USERS)
    return _semaphore


async def acquire_mcp_slot(user_id: str) -> bool:
    """尝试获取 MCP 使用槽位"""
    sem = _get_semaphore()
    async with _lock:
        if user_id in _active_users:
            return True
        if sem.locked() and len(_active_users) >= MAX_CONCURRENT_USERS:
            return False
    await sem.acquire()
    async with _lock:
        _active_users.add(user_id)
    logger.info(f"用户 {user_id} 获取 MCP 槽位 (当前: {len(_active_users)}/{MAX_CONCURRENT_USERS})")
    return True


async def release_mcp_slot(user_id: str):
    """释放 MCP 使用槽位"""
    sem = _get_semaphore()
    async with _lock:
        if user_id in _active_users:
            _active_users.discard(user_id)
            sem.release()
            logger.info(f"用户 {user_id} 释放 MCP 槽位 (当前: {len(_active_users)}/{MAX_CONCURRENT_USERS})")


def get_mcp_status() -> dict:
    """获取 MCP 并发状态"""
    return {
        "max_concurrent": MAX_CONCURRENT_USERS,
        "active_users": len(_active_users),
        "available_slots": MAX_CONCURRENT_USERS - len(_active_users),
        "is_available": len(_active_users) < MAX_CONCURRENT_USERS,
    }


def check_xhs_login_status() -> dict:
    """检查小红书登录状态"""
    try:
        from xiaohongshu_mcp_client import check_login_status
        return check_login_status()
    except ConnectionError:
        return {"logged_in": False, "message": "小红书 MCP 服务未启动"}
    except Exception as e:
        return {"logged_in": False, "message": f"检查失败: {str(e)}"}
