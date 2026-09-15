"""开发调试路由：让后端输出"看得见"
================================================================================

用途：把后端日志（uvicorn 访问日志 + 项目里的 logger + 所有 print）实时推给前端，
在浏览器里就能看到，不必开终端。

  GET /api/dev/logs?tail=300        一次性取最近 N 行（快照）
  GET /api/dev/logs/stream?tail=200 SSE：先发最近 N 行，之后持续跟随新增内容

日志文件由 run_backend.py 写入（stdout/stderr 全部 tee 进去），路径可用环境变量
BACKEND_LOG_FILE 覆盖，默认 <项目根>/logs/backend.log。
"""

import asyncio
import json
import logging
import os
import pathlib
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from auth import get_current_user

logger = logging.getLogger("controller.dev")

router = APIRouter(prefix="/api/dev", tags=["开发调试"])

# 轮询间隔（秒）：日志是本地文件，1s 内的实时性足够
POLL_INTERVAL = float(os.getenv("DEV_LOG_POLL", "1"))
# 空闲多久发一次 SSE 保活注释
PING_INTERVAL = 15.0
# 单次推送的最大行数（防止一次刷出上万行）
MAX_TAIL = 5000


def log_file_path() -> pathlib.Path:
    raw = os.getenv("BACKEND_LOG_FILE", "").strip()
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path(__file__).resolve().parent.parent / "logs" / "backend.log"


def _enabled() -> bool:
    """主应用是否暴露日志接口。

    默认 **关闭**：日志里含请求路径、task_id、用户信息，挂在主应用上等于
    "任何登录用户都能看全站日志"。排查问题请用独立日志服务
    （log_viewer.py：单独端口 + 固定口令，仅你自己可访问）。
    本地调试想临时打开：BACKEND_LOG_ENDPOINT=true。
    """
    return os.getenv("BACKEND_LOG_ENDPOINT", "false").strip().lower() in ("1", "true", "yes", "on")


def _read_tail(path: pathlib.Path, max_lines: int) -> List[str]:
    """从文件尾部按块回溯读取最近 max_lines 行（避免大文件全量读入）。"""
    if not path.exists() or max_lines <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 64 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
    except OSError as exc:
        logger.warning("读取日志文件失败: %s", exc)
        return []
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _event(event_type: str, data) -> str:
    return f"data: {json.dumps({'type': event_type, 'data': data}, ensure_ascii=False)}\n\n"


def require_enabled() -> None:
    """依赖：功能关闭时直接 404（放在鉴权之前，语义更明确）。

    这样无论是否登录，关闭状态下都统一返回 404，而不是"没登录 401、登录了才 404"。
    """
    if not _enabled():
        raise HTTPException(status_code=404, detail="日志接口已关闭（BACKEND_LOG_ENDPOINT=false）")


@router.get("/logs", dependencies=[Depends(require_enabled)])
async def read_logs(
    tail: int = Query(300, ge=1, le=MAX_TAIL),
    current_user: dict = Depends(get_current_user),
):
    """返回日志文件最近 tail 行（供页面首次加载/手动刷新）。"""
    path = log_file_path()
    lines = _read_tail(path, tail)
    return {
        "file": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "lines": lines,
    }


@router.get("/logs/stream", dependencies=[Depends(require_enabled)])
async def stream_logs(
    tail: int = Query(200, ge=0, le=MAX_TAIL),
    current_user: dict = Depends(get_current_user),
):
    """SSE 实时跟随日志文件：先补最近的 tail 行，再持续推送新增内容。"""
    async def event_generator():
        path = log_file_path()
        for line in _read_tail(path, tail):
            yield _event("line", {"text": line})

        try:
            offset = path.stat().st_size if path.exists() else 0
        except OSError:
            offset = 0
        pending = ""
        idle = 0.0
        logger.info("日志流已连接: %s (from=%d)", path, offset)

        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                try:
                    if not path.exists():
                        continue
                    size = path.stat().st_size
                    if size < offset:          # 日志被截断或轮转 → 从头再来
                        offset = 0
                        pending = ""
                    if size > offset:
                        with path.open("r", encoding="utf-8", errors="replace") as fh:
                            fh.seek(offset)
                            chunk = fh.read()
                            offset = fh.tell()
                        pending += chunk
                        *lines, pending = pending.split("\n")
                        for line in lines:
                            yield _event("line", {"text": line})
                        idle = 0.0
                    else:
                        idle += POLL_INTERVAL
                        if idle >= PING_INTERVAL:
                            idle = 0.0
                            yield ": ping\n\n"
                except OSError as exc:
                    yield _event("error", {"message": f"读取日志失败: {exc}"})
                    return
        except asyncio.CancelledError:
            logger.info("日志流断开: %s", path)
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
