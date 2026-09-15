"""聊天 API 路由"""

import asyncio
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from auth import get_current_user
from models.schemas import ChatRequest, ChatResponse, SessionResponse, TripBriefResponse
from services import memory_store
from services.chat_service import chat_with_rag, resolve_chat_mode, stream_chat_with_rag

router = APIRouter(prefix="/api/chat", tags=["聊天"])


def _sse(event_type: str, data) -> str:
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


async def _prepare_request(
    req: ChatRequest,
    user_id: str,
) -> tuple[dict, str, list[dict], dict]:
    """创建/校验会话、获取会话锁并组装记忆上下文。"""
    try:
        session = await memory_store.get_or_create_session(user_id, req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    # 获取会话锁，防止并发请求导致记忆上下文不一致 即同一时间只能有一个请求在处理同一会话
    lock_token = await memory_store.acquire_session_lock(session["session_id"])
    if not lock_token:
        raise HTTPException(status_code=429, detail="当前会话正在处理中，请稍后再试")

    try:
        context = await memory_store.build_context(
            user_id=user_id,
            session_id=session["session_id"],
            query=req.message,
        )
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in context["recent_messages"]
            if item["role"] in {"user", "assistant"}
        ]
        return session, lock_token, history, context
    except Exception:
        await memory_store.release_session_lock(session["session_id"], lock_token)
        raise


async def _persist_turn(
    session_id: str,
    user_id: str,
    request_id: str,
    message: str,
    reply: str,
) -> None:
    """写入用户与助手消息，随后调度异步记忆处理。"""
    await memory_store.append_message(
        session_id=session_id,
        user_id=user_id,
        role="user",
        content=message,
        request_id=request_id,
        metadata={"source": "chat"},
    )
    await memory_store.append_message(
        session_id=session_id,
        user_id=user_id,
        role="assistant",
        content=reply,
        request_id=f"{request_id}:assistant",
        metadata={"source": "chat"},
    )
    memory_store.schedule_memory_processing(session_id, user_id)


@router.post("/message", response_model=ChatResponse)
async def send_message(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """发送聊天消息（旅游限定 + 长短期记忆 + RAG）"""
    session, lock_token, history, context = await _prepare_request(req, current_user["user_id"])
    mode = resolve_chat_mode(req.chat_mode, req.history_mode)
    try:
        result = await chat_with_rag(
            user_id=current_user["user_id"],
            username=current_user["username"],
            message=req.message,
            history=history,
            system_context=context["memory_context"],
            history_mode=req.history_mode,
            chat_mode=req.chat_mode,
        )
        request_id = req.request_id or uuid.uuid4().hex
        await _persist_turn(
            session_id=session["session_id"],
            user_id=current_user["user_id"],
            request_id=request_id,
            message=req.message,
            reply=result.get("reply", ""),
        )
        brief = await memory_store.get_trip_brief(session["session_id"])
        return ChatResponse(
            reply=result["reply"],
            session_id=session["session_id"],
            used_rag=result.get("used_rag", False),
            chat_mode=result.get("chat_mode", mode),
            history_mode=result.get("history_mode", mode == "history"),
            rag_decision=result.get("rag_decision"),
            related_guides=result.get("related_guides", []),
            trip_brief=brief.get("brief") or None,
            next_questions=[],
        )
    except memory_store.DuplicateRequestError as exc:
        raise HTTPException(status_code=409, detail="请求已处理，请勿重复提交") from exc
    finally:
        await memory_store.release_session_lock(session["session_id"], lock_token)


@router.post("/stream")
async def send_message_stream(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """聊天流式输出（SSE）：chunk 实时推送，done 返回完整结果"""
    session, lock_token, history, context = await _prepare_request(req, current_user["user_id"])
    mode = resolve_chat_mode(req.chat_mode, req.history_mode)

    async def event_generator():
        try:
            async for event in stream_chat_with_rag(
                user_id=current_user["user_id"],
                username=current_user["username"],
                message=req.message,
                history=history,
                system_context=context["memory_context"],
                history_mode=req.history_mode,
                chat_mode=req.chat_mode,
            ):
                if event["type"] != "done":
                    yield _sse(event["type"], event["data"])
                    continue

                reply = event["data"].get("reply", "")
                request_id = req.request_id or uuid.uuid4().hex
                await _persist_turn(
                    session_id=session["session_id"],
                    user_id=current_user["user_id"],
                    request_id=request_id,
                    message=req.message,
                    reply=reply,
                )
                brief = await memory_store.get_trip_brief(session["session_id"])
                done_data = {
                    **event["data"],
                    "session_id": session["session_id"],
                    "chat_mode": event["data"].get("chat_mode", mode),
                    "history_mode": event["data"].get("history_mode", mode == "history"),
                    "trip_brief": brief.get("brief") or None,
                    "next_questions": [],
                }
                yield _sse("done", done_data)
        except memory_store.DuplicateRequestError as exc:
            yield _sse("error", {"message": "请求已处理，请勿重复提交", "code": 409})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield _sse("error", {"message": str(exc)})
        finally:
            await memory_store.release_session_lock(session["session_id"], lock_token)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def get_sessions(
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """获取当前用户的会话列表"""
    return await memory_store.list_sessions(current_user["user_id"], limit)


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(100, ge=1, le=200),
    before_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user),
):
    """获取指定会话的消息，支持向上翻页"""
    try:
        await memory_store.get_or_create_session(current_user["user_id"], session_id)
        messages = await memory_store.get_messages(session_id, limit, before_id)
        return {"messages": messages}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/planning", response_model=TripBriefResponse)
async def get_session_planning(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """获取指定会话的结构化行程简报"""
    try:
        await memory_store.get_or_create_session(current_user["user_id"], session_id)
        return await memory_store.get_trip_brief(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """删除指定会话及其记忆数据"""
    deleted = await memory_store.delete_session(session_id, current_user["user_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在或无权删除")
    return {"deleted": True}


@router.get("/history")
async def get_history(
    session_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    """获取聊天历史；未传 session_id 时返回最近会话"""
    if session_id:
        target_session_id = session_id
    else:
        latest = await memory_store.get_latest_session(current_user["user_id"])
        if not latest:
            return {"session_id": None, "messages": []}
        target_session_id = latest["session_id"]
    messages = await memory_store.get_messages(target_session_id, limit)
    return {"session_id": target_session_id, "messages": messages}
