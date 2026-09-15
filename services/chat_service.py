"""聊天服务 —— 旅游限定聊天 + 可选 RAG（历史模式 / 公开模式）

聊天模式（chat_mode，三选一）：

    normal   普通（默认）：不做关键词检索、不做意图判定，直接把问题交给模型回答
    history  历史模式    ：关键词 + LLM 意图判定（skills/rag-intent.md），判定需要时
                           **只检索当前用户自己的攻略**
    public   公开模式    ：不做意图判定，**检索"自己的 + 他人已公开的"攻略**作为参考

事件流（供 SSE 透传）：
    {"type": "start", "data": {chat_mode, used_rag, rag_decision, related_guides}}
    {"type": "chunk", "data": "文本片段"}
    {"type": "done",  "data": {reply, chat_mode, used_rag, rag_decision, related_guides}}
    {"type": "error", "data": {message}}
"""

import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from agents.chat_agent import ChatAgent
from services.es_client import save_chat_message
from services.rag_service import (
    agent_rag_intent,
    build_rag_context,
    chat_visibility,
    mode_visibility,
    search_related_guides_with_meta,
)

logger = logging.getLogger(__name__)

CHAT_MODES = ("normal", "history", "public")


def resolve_chat_mode(chat_mode: Optional[str] = None, history_mode: bool = False) -> str:
    """把 (chat_mode, history_mode) 归一成三模式之一（history_mode 为兼容字段）。"""
    if chat_mode in CHAT_MODES:
        return str(chat_mode)
    return "history" if history_mode else "normal"

TRAVEL_SYSTEM_PROMPT = """你是一个专业的旅游规划助手。你只回答与旅游相关的问题，包括：
- 旅游目的地推荐
- 行程规划建议
- 景点介绍
- 美食推荐
- 住宿建议
- 交通方式
- 旅行预算
- 旅行注意事项

如果用户的问题与旅游无关，请礼貌地告知用户你只能回答旅游相关的问题，并引导用户提出旅游相关的疑问。

回答时请结合提供的历史攻略信息（如有），给出实用、具体的建议。"""


def _guide_brief(guides: List[dict], owner_user_id: Optional[str] = None) -> List[dict]:
    """给前端的精简引用信息。owner_user_id 用于标注「我的 / 公开」。"""
    briefs = []
    for g in guides:
        briefs.append({
            "guide_id": g.get("guide_id", g.get("_id", "")),
            "title": g.get("title", ""),
            "destination": g.get("destination", ""),
            "score": round(float(g.get("_score") or 0.0), 4),
            "channels": g.get("channels", []),
            # 公开模式下前端要能区分"我的攻略"与"别人公开的"
            "is_mine": None if owner_user_id is None else (g.get("user_id") == owner_user_id),
            "is_public": bool(g.get("is_public")),
            "author": g.get("nickname") or g.get("username") or "",
        })
    return briefs


async def _resolve_rag(message: str, mode: str,
                       user_id: Optional[str] = None) -> Dict[str, Any]:
    """按聊天模式处理 RAG：普通不检索；历史=只看自己的；公开=自己的 + 他人公开的。

    · history：关键词快筛 + LLM 意图判定（skills/rag-intent.md）→ 判定需要才检索
    · public ：不做意图判定，直接把问题当检索语句，检索"自己的 + 他人已公开的"攻略

    user_id 必须带上：拿不到就跳过检索（绝不退回"全库可见"，避免把别人的或
    测试写入的攻略当成本人历史注入上下文）。

    返回 {"decision": {...}, "guides": [...], "context": str, "meta": {...}}
    """
    decision: Dict[str, Any] = {
        "mode": mode,
        "chat_mode": mode,
        "history_mode": mode == "history",
        "need_rag": False,
        "intent_type": "skipped",
        "reason": "普通模式：跳过关键词检索与意图判定，直接由模型回答",
        "query": "",
        "keywords": [],
        "source": "off",
    }
    if mode == "normal":
        return {"decision": decision, "guides": [], "context": "", "meta": {}}

    if not user_id:
        logger.warning("聊天 RAG 未拿到 user_id：为避免检索到他人攻略，本次跳过检索")
        decision.update({
            "need_rag": False, "source": "no_user",
            "reason": "缺少用户身份（user_id），跳过检索以避免命中他人攻略",
        })
        return {"decision": decision, "guides": [], "context": "", "meta": {}}

    visibility = mode_visibility(mode)
    decision["visibility"] = visibility

    if mode == "history":
        try:
            decision = {**decision, **await agent_rag_intent(message)}   # 关键词快路径 + LLM 语义判定
        except Exception as exc:                                        # 判定失败不影响正常回答
            logger.warning(f"历史模式意图判定失败: {exc}")
            decision.update({"need_rag": False, "reason": f"意图判定失败：{exc}",
                             "source": "error"})
            return {"decision": decision, "guides": [], "context": "", "meta": {}}
        decision.update({"mode": mode, "chat_mode": mode, "history_mode": True,
                         "visibility": visibility})
        if not decision.get("need_rag"):
            return {"decision": decision, "guides": [], "context": "", "meta": {}}
        search_query = decision.get("query") or message
    else:
        # 公开模式：不做意图判定，直接检索攻略库（自己的 + 他人已公开的）
        decision.update({
            "need_rag": True,
            "intent_type": "knowledge_base",
            "source": "public",
            "query": message,
            "reason": "公开模式：检索「我的 + 他人已公开」的攻略作为参考",
        })
        search_query = message

    try:
        guides, meta = search_related_guides_with_meta(
            search_query, top_k=3, user_id=user_id, visibility=visibility)
    except Exception as exc:
        logger.warning(f"聊天 RAG 检索失败: {exc}")
        decision["reason"] = f"{decision.get('reason', '')}（检索失败：{exc}）"
        return {"decision": decision, "guides": [], "context": "", "meta": {}}

    decision["visibility"] = meta.get("visibility", visibility)
    context = build_rag_context(guides, owner_user_id=user_id, mode=mode) if guides else ""
    if guides:
        own = sum(1 for g in guides if g.get("user_id") == user_id)
        logger.info(
            f"{mode} 模式：RAG 命中 {len(guides)} 条（自己的 {own} 条 / 公开 {len(guides) - own} 条）"
            f"｜可见性={decision['visibility']}｜user_id={user_id}")
    else:
        logger.info(f"{mode} 模式：未命中攻略（可见性={decision['visibility']} user_id={user_id}）")
    return {"decision": decision, "guides": guides, "context": context, "meta": meta}


async def stream_chat_with_rag(
    user_id: str,
    username: str,
    message: str,
    history: Optional[List[dict]] = None,
    system_context: Optional[str] = None,
    history_mode: bool = False,
    chat_mode: Optional[str] = None,
) -> AsyncIterator[dict]:
    """流式处理聊天请求（含可选 RAG：历史模式 / 公开模式）。"""
    mode = resolve_chat_mode(chat_mode, history_mode)
    rag = await _resolve_rag(message, mode, user_id)
    related_guides = rag["guides"]
    rag_context = rag["context"]
    decision = rag["decision"]
    used_rag = bool(related_guides)
    guide_refs = [g.get("guide_id", g.get("_id", "")) for g in related_guides]

    yield {
        "type": "start",
        "data": {
            "chat_mode": mode,
            "history_mode": mode == "history",
            "used_rag": used_rag,
            "rag_decision": decision,
            "related_guides": _guide_brief(related_guides, user_id),
        },
    }

    system_content = TRAVEL_SYSTEM_PROMPT
    if rag_context:
        system_content += f"\n\n{rag_context}"
    if system_context:
        system_content += f"\n\n{system_context}"

    messages = [{"role": "system", "content": system_content}]

    if history:
        for msg in history[-10:]:
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    messages.append({"role": "user", "content": message})

    agent = ChatAgent(system_prompt=system_content)
    user_messages = [m for m in messages if m["role"] != "system"]
    reply_parts = []

    try:
        async for chunk in agent.chat_stream(user_messages):
            if chunk:
                reply_parts.append(chunk)
                yield {"type": "chunk", "data": chunk}
    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        if not reply_parts:
            reply_parts.append("抱歉，我暂时无法回答，请稍后再试。")
            yield {"type": "chunk", "data": reply_parts[0]}

    reply = "".join(reply_parts)
    now = datetime.now(timezone.utc).isoformat()
    try:
        save_chat_message({
            "user_id": user_id,
            "role": "user",
            "content": message,
            "guide_refs": guide_refs,
            "created_at": now,
        })
        save_chat_message({
            "user_id": user_id,
            "role": "assistant",
            "content": reply,
            "guide_refs": [],
            "created_at": now,
        })
    except Exception as e:
        logger.warning(f"保存聊天记录失败: {e}")

    yield {
        "type": "done",
        "data": {
            "reply": reply,
            "chat_mode": mode,
            "history_mode": mode == "history",
            "used_rag": used_rag,
            "rag_decision": decision,
            "related_guides": _guide_brief(related_guides, user_id),
        },
    }


async def chat_with_rag(
    user_id: str,
    username: str,
    message: str,
    history: Optional[List[dict]] = None,
    system_context: Optional[str] = None,
    history_mode: bool = False,
    chat_mode: Optional[str] = None,
) -> dict:
    """非流式聊天：消费流式事件并返回完整回复（兼容原接口）"""
    mode = resolve_chat_mode(chat_mode, history_mode)
    reply_parts = []
    result = None

    async for event in stream_chat_with_rag(
        user_id=user_id,
        username=username,
        message=message,
        history=history,
        system_context=system_context,
        history_mode=history_mode,
        chat_mode=mode,
    ):
        if event["type"] == "chunk":
            reply_parts.append(event["data"])
        elif event["type"] == "done":
            result = event["data"]

    if result is None:
        result = {
            "reply": "".join(reply_parts) or "抱歉，我暂时无法回答，请稍后再试。",
            "chat_mode": mode,
            "history_mode": mode == "history",
            "used_rag": False,
            "rag_decision": {"mode": mode, "chat_mode": mode,
                             "history_mode": mode == "history", "need_rag": False,
                             "reason": "未完成检索判定", "source": "off"},
            "related_guides": [],
        }

    return result
