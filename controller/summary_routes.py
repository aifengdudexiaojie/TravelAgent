"""总结分析路由 - 阶段2：SSE 推送分析进度 + 最终总结"""

import json
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from utils.redis_storage import RedisMemory


def get_redis():
    return RedisMemory()


router = APIRouter(prefix="/api/summary", tags=["总结分析"])


class SummaryRequest(BaseModel):
    task_id: int


def _sse(event_type: str, data) -> str:
    """构造 SSE 事件"""
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


@router.post("/stream")
async def summary_stream(req: SummaryRequest, redis: RedisMemory = Depends(get_redis)):
    """
    阶段2：确认意图后，执行总结分析，SSE 实时推送进度和最终结果。

    事件流：
      - {"type": "start", "data": {...}}         分析开始
      - {"type": "address_start", "data": {...}}  开始分析某个地点
      - {"type": "post_start", "data": {...}}     开始分析某篇帖子
      - {"type": "address_done", "data": {...}}   某个地点完成
      - {"type": "done", "data": final_summary}   全部完成，返回最终总结
      - {"type": "error", "data": {...}}          出错
    """
    from functions.summary_from_notes import summary_from_notes

    async def event_generator():
        try:
            # 【修改③】消费 summary_from_notes 生成器，逐个透传进度事件到 SSE
            print(f"当前传入task_id={req.task_id}")
            async for event in summary_from_notes(req.task_id, redis):
                yield _sse(event["type"], event["data"])
        except Exception as e:
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
