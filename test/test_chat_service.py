"""聊天服务与聊天接口的单元测试（全部 mock，不需要 PG / Redis / LLM）。

覆盖：
  · chat_with_rag           —— 流式分片被正确拼成完整回复
  · /api/chat/stream        —— SSE 端点事件序列与 done 字段（memory_store 用内存假实现）
  · resolve_chat_mode()     —— 三种模式的归一化（含 history_mode 兼容字段）
  · _resolve_rag()          —— 普通模式必须完全不碰检索
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.chat_agent import ChatAgent
import controller.chat_routes as chat_routes
from services.chat_service import chat_with_rag, resolve_chat_mode


async def _fake_chat_stream(self, messages):
    """替掉真实 LLM：固定回三个分片。"""
    for chunk in ["你", "好", "！"]:
        yield chunk


class FakeMemoryStore:
    """内存版会话存储：让路由测试不依赖 PostgreSQL / Redis。"""

    class DuplicateRequestError(Exception):
        pass

    def __init__(self):
        self.appended = []

    async def get_or_create_session(self, user_id, session_id=None):
        return {"session_id": session_id or "s-test", "user_id": user_id}

    async def acquire_session_lock(self, session_id):
        return "lock-token"

    async def release_session_lock(self, session_id, token):
        return None

    async def build_context(self, user_id, session_id, query):
        return {"recent_messages": [], "memory_context": ""}

    async def append_message(self, **kwargs):
        self.appended.append(kwargs)

    def schedule_memory_processing(self, session_id, user_id):
        return None

    async def get_trip_brief(self, session_id):
        return {"brief": None}


class ChatServiceTest(unittest.TestCase):
    def test_chat_with_rag_consumes_stream(self):
        async def run():
            with patch.object(ChatAgent, "chat_stream", _fake_chat_stream), \
                 patch("services.chat_service.save_chat_message") as save:
                result = await chat_with_rag(
                    user_id="u1",
                    username="alice",
                    message="你好",
                    history=[],
                )
                return result, save

        result, save = asyncio.run(run())
        self.assertEqual(result["reply"], "你好！")
        self.assertEqual(result["used_rag"], False)
        self.assertEqual(result["chat_mode"], "normal")
        # 用户消息 + 助手回复各存一条
        self.assertEqual(save.call_count, 2)

    def test_stream_endpoint_returns_sse(self):
        recorded = {}

        async def fake_stream(user_id, username, message, history=None,
                             system_context=None, history_mode=False, chat_mode=None):
            recorded.update({"user_id": user_id, "chat_mode": chat_mode, "message": message})
            yield {"type": "start", "data": {"chat_mode": chat_mode or "normal",
                                             "used_rag": False, "related_guides": []}}
            yield {"type": "chunk", "data": "你"}
            yield {"type": "chunk", "data": "好"}
            yield {"type": "done", "data": {"reply": "你好", "chat_mode": chat_mode or "normal",
                                            "used_rag": False, "related_guides": []}}

        async def fake_current_user(credentials=None):
            return {"user_id": "u1", "username": "alice"}

        fake_store = FakeMemoryStore()
        with patch.object(chat_routes, "stream_chat_with_rag", fake_stream), \
             patch.object(chat_routes, "memory_store", fake_store):
            app = FastAPI()
            app.include_router(chat_routes.router)
            app.dependency_overrides[chat_routes.get_current_user] = fake_current_user
            client = TestClient(app)
            resp = client.post("/api/chat/stream",
                               json={"message": "你好", "history": [], "chat_mode": "public"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("data: ", resp.text)
        self.assertIn("你好", resp.text)
        self.assertIn('"chat_mode": "public"', resp.text)
        # 透传给 service 的参数
        self.assertEqual(recorded["chat_mode"], "public")
        self.assertEqual(recorded["user_id"], "u1")
        # 会话落库：用户消息 + 助手消息
        self.assertEqual(len(fake_store.appended), 2)

    def test_sse_keeps_crlf_free_single_frame(self):
        """每个事件都是 `data: {...}\\n\\n` 的独立帧（前端按 \\n\\n 切分）。"""
        async def fake_stream(user_id, username, message, history=None,
                             system_context=None, history_mode=False, chat_mode=None):
            yield {"type": "done", "data": {"reply": "ok", "chat_mode": "normal",
                                            "used_rag": False, "related_guides": []}}

        async def fake_current_user(credentials=None):
            return {"user_id": "u1", "username": "alice"}

        with patch.object(chat_routes, "stream_chat_with_rag", fake_stream), \
             patch.object(chat_routes, "memory_store", FakeMemoryStore()):
            app = FastAPI()
            app.include_router(chat_routes.router)
            app.dependency_overrides[chat_routes.get_current_user] = fake_current_user
            resp = TestClient(app).post("/api/chat/stream", json={"message": "hi"})

        frames = [f for f in resp.text.split("\n\n") if f.strip()]
        self.assertTrue(frames)
        for frame in frames:
            self.assertTrue(frame.startswith("data: "), frame)


class ChatModeTest(unittest.TestCase):
    """三模式归一化：chat_mode 优先，未传时才看兼容字段 history_mode。"""

    def test_resolve_chat_mode(self):
        self.assertEqual(resolve_chat_mode(None, False), "normal")
        self.assertEqual(resolve_chat_mode(None, True), "history")
        self.assertEqual(resolve_chat_mode("public", False), "public")
        self.assertEqual(resolve_chat_mode("history", True), "history")
        # 显式传 normal 时，history_mode 不应把它顶掉
        self.assertEqual(resolve_chat_mode("normal", True), "normal")
        # 非法值回退
        self.assertEqual(resolve_chat_mode("whatever", False), "normal")

    def test_normal_mode_never_touches_retrieval(self):
        from services.chat_service import _resolve_rag

        with patch("services.chat_service.search_related_guides_with_meta",
                   side_effect=AssertionError("普通模式不该检索")) as search, \
             patch("services.chat_service.agent_rag_intent",
                   side_effect=AssertionError("普通模式不该做意图判定")) as intent:
            result = asyncio.run(_resolve_rag("你好", "normal", "u1"))

        self.assertEqual(result["decision"]["source"], "off")
        self.assertEqual(result["guides"], [])
        search.assert_not_called()
        intent.assert_not_called()

    def test_history_mode_requires_user_id(self):
        from services.chat_service import _resolve_rag

        with patch("services.chat_service.search_related_guides_with_meta") as search:
            result = asyncio.run(_resolve_rag("上次去西安", "history", None))

        # 没有 user_id 时必须跳过检索（而不是退化成"全库可见"）
        self.assertEqual(result["decision"]["source"], "no_user")
        self.assertEqual(result["guides"], [])
        search.assert_not_called()

    def test_mode_visibility_mapping(self):
        from services.rag_service import mode_visibility

        self.assertEqual(mode_visibility("history"), "own")
        self.assertEqual(mode_visibility("public"), "own_or_public")


if __name__ == "__main__":
    unittest.main()
