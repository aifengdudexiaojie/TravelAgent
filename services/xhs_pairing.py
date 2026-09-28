"""配对码：让「本机登录助手」在不接触 JWT 的前提下把登录态交给服务器
================================================================================

为什么需要：用户在自己电脑上登录小红书（自己的设备/IP，不触发云端风控），
然后用一个小工具把登录态传上来。工具在用户机器上跑，天然拿不到网页里的登录 token，
所以用"配对码"这种短时凭据：

    网页（已登录本系统）→ 生成配对码（8 位、10 分钟、一次性）
    本机助手           → 带上配对码把 cookies 传上来 → 服务器写进该用户目录

安全约束：
  · 码只在本进程内存里（单副本部署的前提不变），10 分钟过期、用一次即销毁；
  · 按来源 IP 限制失败次数，避免有人暴力猜码；
  · 上传内容仍走 import_cookies 的校验（合法 JSON、≤2MB、必须含 cookie 结构）。
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("services.xhs_pairing")

TTL_SECONDS = 600                     # 配对码有效期
MAX_FAILS_PER_IP = 8                  # 同一 IP 最多试错几次
CODE_LEN = 8
# 去掉 0/O/1/I/L 这类容易看错的字符，用户要手抄或复制
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_lock = threading.RLock()
_codes: Dict[str, Dict[str, Any]] = {}          # code -> {user_id, expires_at}
_fails: Dict[str, List[float]] = {}             # ip -> [失败时间戳]


def _reap(now: Optional[float] = None) -> None:
    now = now or time.time()
    for code, item in list(_codes.items()):
        if item["expires_at"] < now:
            _codes.pop(code, None)
    for ip, stamps in list(_fails.items()):
        fresh = [t for t in stamps if now - t < 3600]
        if fresh:
            _fails[ip] = fresh
        else:
            _fails.pop(ip, None)


def _new_code() -> str:
    while True:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))
        if code not in _codes:
            return code


def issue(user_id: str) -> Dict[str, Any]:
    """给当前登录用户生成一个配对码（同一用户重复调用会作废旧码）。"""
    with _lock:
        _reap()
        for code, item in list(_codes.items()):
            if item["user_id"] == user_id:
                _codes.pop(code, None)
        code = _new_code()
        _codes[code] = {"user_id": user_id, "expires_at": time.time() + TTL_SECONDS}
    logger.info("已生成登录助手配对码：user=%s（%d 分钟内有效）", user_id, TTL_SECONDS // 60)
    return {"code": code, "expires_in": TTL_SECONDS}


def too_many_fails(ip: str) -> bool:
    with _lock:
        _reap()
        return len(_fails.get(ip, [])) >= MAX_FAILS_PER_IP


def _note_fail(ip: str) -> None:
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def consume(code: str, ip: str = "") -> Optional[str]:
    """校验并销毁配对码，返回 user_id；失败返回 None。"""
    code = (code or "").strip().upper()
    with _lock:
        _reap()
        item = _codes.pop(code, None)
        if item is None:
            _note_fail(ip or "-")
            logger.warning("配对码校验失败：ip=%s code=%s", ip or "-", code[:4] + "****")
            return None
        # 成功后清掉该 IP 的失败记录
        _fails.pop(ip or "-", None)
    logger.info("配对码已使用：user=%s", item["user_id"])
    return item["user_id"]


def pending_count() -> int:
    with _lock:
        _reap()
        return len(_codes)
