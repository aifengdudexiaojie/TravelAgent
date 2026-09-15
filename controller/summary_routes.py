"""总结分析路由 - 阶段2：SSE 推送分析进度 + 最终总结 + 自动写入 RAG
================================================================================

任务模型（关键改动）：
    分析不再跑在 HTTP 请求里，而是由 services/summary_task.py 启动一个**后台任务**。
    因此：切页面、刷新、关标签页都不会中断分析；重新连上时从缓冲区重放全部进度。

事件流：
  - {"type": "start", "data": {...}}            分析开始
  - {"type": "address_start", "data": {...}}    开始分析某个地点
  - {"type": "post_start", "data": {...}}       开始分析某篇帖子
  - {"type": "address_done", "data": {...}}     某个地点完成
  - {"type": "done", "data": final_summary}     全部完成，返回最终总结对象
  - {"type": "saving", "data": {...}}           正在写入 RAG（PG 真相源 / ES / Qdrant）
  - {"type": "saved", "data": {...}}            写入完成，含结构校验报告与 guide_id
  - {"type": "validation_error", "data": {...}} 总结 JSON/结构校验未通过（复杂问题，**未写入 RAG**）
                                                data.kind = format | structure
  - {"type": "save_error", "data": {...}}       写入过程出错（不影响已返回的总结）
  - {"type": "error", "data": {...}}            分析流程出错

接口：
  POST /api/summary/stream          订阅进度（幂等启动任务；from_index=0 时重放全部历史事件）
  GET  /api/summary/task/{task_id}  任务快照（status/events/final_summary，用于刷新后追平）
  POST /api/summary/cancel/{task_id} 取消正在跑的分析

RAG 入库与校验开关：
  RAG_AUTO_INGEST=true|false     （默认 true；请求体 save_to_rag 可单次覆盖）
  RAG_VALIDATE_STRICT=true|false （默认 true：复杂问题抛异常并跳过入库；
                                  false：仅记录 report 并跳过入库，不抛异常）
"""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.summary_task import (      # noqa: F401  save_summary_to_rag 在此重导出，兼容旧引用
    cancel_task,
    save_summary_to_rag,
    snapshot_task,
    start_summary_task,
)

logger = logging.getLogger("controller.summary")

router = APIRouter(prefix="/api/summary", tags=["总结分析"])


class SummaryRequest(BaseModel):
    task_id: int
    # 从第几个事件开始订阅：0（默认）= 重放全部历史进度，适合刷新/切页后重新挂载
    from_index: int = 0
    # 以下均为可选：用于把生成的攻略归属到具体用户；不传则用环境变量默认归属
    user_id: str | None = None
    username: str | None = None
    nickname: str | None = None
    is_public: bool = False
    # 单次覆盖自动入库开关（None = 按环境变量 RAG_AUTO_INGEST）
    save_to_rag: bool | None = None


def _sse(event_type: str, data) -> str:
    """构造 SSE 事件"""
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


def _owner(req: SummaryRequest) -> dict:
    return {
        "user_id": req.user_id,
        "username": req.username,
        "nickname": req.nickname,
        "is_public": req.is_public,
        "save_to_rag": req.save_to_rag,
    }


@router.post("/stream")
async def summary_stream(req: SummaryRequest):
    """订阅分析进度（SSE）。

    幂等：同一个 task_id 只会真正分析一次；重复调用（前端重连/切页回来/刷新）
    只是挂到同一个后台任务上，从 from_index 开始重放并继续跟随。
    """
    task = start_summary_task(req.task_id, _owner(req))
    logger.info(
        "SSE 订阅 task_id=%s status=%s 已有事件=%d from_index=%d",
        req.task_id, task.status, len(task.events), req.from_index,
    )

    async def event_generator():
        try:
            async for event in task.subscribe(from_index=max(0, req.from_index)):
                if event.get("type") == "ping":
                    yield ": ping\n\n"          # SSE 注释行：保活，前端会自动忽略
                    continue
                yield _sse(event["type"], event["data"])
        except asyncio.CancelledError:
            # 客户端断开：只结束这次订阅，后台分析继续跑（这正是"切页面进度不丢"的关键）
            logger.info("客户端断开订阅，分析继续在后台执行 (task_id=%s)", req.task_id)
            raise
        finally:
            logger.info("[SSE] 事件流结束 (task_id=%s status=%s)", req.task_id, task.status)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/task/{task_id}")
async def summary_task_snapshot(task_id: int):
    """任务快照：状态 + 已产生的事件 + 最终总结（刷新页面后用它一次性追平）。"""
    snapshot = snapshot_task(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新识别意图")
    return snapshot


@router.post("/cancel/{task_id}")
async def summary_task_cancel(task_id: int):
    """取消正在执行的分析任务。"""
    ok = await cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="任务不存在或已结束")
    return {"cancelled": True, "task_id": task_id}
