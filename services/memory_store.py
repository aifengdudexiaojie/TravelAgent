"""PostgreSQL + Qdrant 长短期记忆存储"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
import uuid
from typing import Any, Dict, List, Optional

import asyncpg
import redis.asyncio as redis
from qdrant_client import AsyncQdrantClient, models

from agents.chat_agent import ChatAgent
from services.embedding import embed_text

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/travel_agent")
DATABASE_POOL_SIZE = int(os.getenv("DATABASE_POOL_SIZE", "10"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION_NAME", "travel_agent_memories")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

MEMORY_MESSAGE_LIMIT = int(os.getenv("MEMORY_MESSAGE_LIMIT", "12"))
MEMORY_SEARCH_LIMIT = int(os.getenv("MEMORY_SEARCH_LIMIT", "5"))
MEMORY_SUMMARY_EVERY_MESSAGES = int(os.getenv("MEMORY_SUMMARY_EVERY_MESSAGES", "10"))
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
SCHEMA_VERSION = 1

SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '新的旅行对话',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
    ON chat_sessions (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    request_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_seq
    ON chat_messages (session_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_request_id
    ON chat_messages (session_id, request_id)
    WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_summaries (
    session_id UUID PRIMARY KEY REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    last_message_id BIGINT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'preference',
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.7,
    status TEXT NOT NULL DEFAULT 'active',
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    content_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    source_session_id UUID,
    source_message_ids BIGINT[] NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_items_user
    ON memory_items (user_id, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_active_content
    ON memory_items (user_id, memory_type, content_hash)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS trip_briefs (
    session_id UUID PRIMARY KEY REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    brief JSONB NOT NULL DEFAULT '{}'::jsonb,
    version INTEGER NOT NULL DEFAULT 1,
    source_message_id BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_pool: Optional[asyncpg.Pool] = None
_qdrant: Optional[AsyncQdrantClient] = None
_redis: Optional[redis.Redis] = None
_background_tasks = set()

_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

_MEMORY_EXTRACTION_PROMPT = """你是旅行助手记忆抽取器。请只输出 JSON，不要输出解释。
从用户输入和助手回复中抽取可持续使用的旅行记忆。
memory_type 只能是 preference、constraint、trip_fact。
不要把一次性的天气提醒或临时计算作为长期记忆。

JSON 格式：
{
  "memories": [
    {
      "content": "用户希望预算低于5000元",
      "memory_type": "constraint",
      "confidence": 0.9
    }
  ],
  "trip_brief": {
    "destination": "成都",
    "dates": null,
    "travelers": null,
    "budget": null,
    "interests": [],
    "transportation": null,
    "accommodation": null,
    "dietary_restrictions": [],
    "must_visit": [],
    "avoid": []
  },
  "next_questions": ["旅行日期是哪几天？"]
}
"""


class DuplicateRequestError(Exception):
    """同一会话内重复提交的请求。"""


async def _init_connection(connection: asyncpg.Connection) -> None:
    """让 asyncpg 自动把 JSONB 字段解析为 dict/list。"""
    await connection.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


def content_hash(text: str) -> str:
    normalized = " ".join((text or "").strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def metadata_hash(metadata: Dict[str, Any]) -> str:
    stable = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def initialize() -> None:
    """初始化 PostgreSQL、Qdrant 与 Redis 连接。"""
    global _pool, _qdrant, _redis

    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=DATABASE_POOL_SIZE,
            init=_init_connection,
        )
        await _pool.execute(SQL_SCHEMA)
        logger.info("PostgreSQL memory pool initialized")

    if _qdrant is None:
        _qdrant = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        if not await _qdrant.collection_exists(QDRANT_COLLECTION):
            await _qdrant.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=models.VectorParams(
                    size=EMBEDDING_DIM,
                    distance=models.Distance.COSINE,
                ),
            )
        for field, field_type in (
            ("user_id", models.PayloadSchemaType.KEYWORD),
            ("status", models.PayloadSchemaType.KEYWORD),
            ("memory_type", models.PayloadSchemaType.KEYWORD),
        ):
            try:
                await _qdrant.create_payload_index(
                    collection_name=QDRANT_COLLECTION,
                    field_name=field,
                    field_schema=field_type,
                )
            except Exception as exc:
                logger.debug("Qdrant index %s may already exist: %s", field, exc)
        logger.info("Qdrant memory collection initialized")

    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        logger.info("Redis memory lock initialized")


async def close() -> None:
    """释放后台任务和数据库连接。"""
    global _pool, _qdrant, _redis

    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    if _qdrant is not None:
        await _qdrant.close()
        _qdrant = None
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    if _pool is not None:
        await _pool.close()
        _pool = None


def _session_lock_key(session_id: str) -> str:
    return f"chat:session:{session_id}:lock"


async def acquire_session_lock(session_id: str) -> Optional[str]:
    """获取会话锁，避免同一会话并发请求交错写入。"""
    if _redis is None:
        raise RuntimeError("memory store not initialized")
    token = uuid.uuid4().hex
    acquired = await _redis.set(
        _session_lock_key(session_id),
        token,
        nx=True,
        px=90_000,
    )
    return token if acquired else None


async def release_session_lock(session_id: str, token: Optional[str]) -> None:
    """只允许锁持有者释放锁。"""
    if _redis is None or not token:
        return
    try:
        await _redis.eval(_RELEASE_LOCK_SCRIPT, 1, _session_lock_key(session_id), token)
    except Exception as exc:
        logger.warning("Release session lock failed: %s", exc)


def _pool_or_error() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("memory store not initialized")
    return _pool


def _row_to_session(row: asyncpg.Record) -> Dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "user_id": row["user_id"],
        "title": row["title"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _row_to_message(row: asyncpg.Record) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": str(row["session_id"]),
        "role": row["role"],
        "content": row["content"],
        "request_id": row["request_id"],
        "metadata": row["metadata"] or {},
        "created_at": row["created_at"].isoformat(),
    }


async def get_or_create_session(
    user_id: str,
    session_id: Optional[str] = None,
    title: str = "新的旅行对话",
) -> Dict[str, Any]:
    """获取指定会话；未提供 session_id 时创建新会话。"""
    pool = _pool_or_error()
    async with pool.acquire() as connection:
        if session_id:
            try:
                parsed_session_id = uuid.UUID(session_id)
            except ValueError as exc:
                raise ValueError("session_id 格式错误") from exc
            row = await connection.fetchrow(
                "SELECT * FROM chat_sessions WHERE session_id = $1",
                parsed_session_id,
            )
            if not row:
                raise ValueError("会话不存在")
            if row["user_id"] != user_id:
                raise PermissionError("无权访问该会话")
            return _row_to_session(row)

        row = await connection.fetchrow(
            """
            INSERT INTO chat_sessions (session_id, user_id, title)
            VALUES ($1, $2, $3)
            RETURNING *
            """,
            uuid.uuid4(),
            user_id,
            title,
        )
        return _row_to_session(row)


async def append_message(
    session_id: str,
    user_id: str,
    role: str,
    content: str,
    request_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """按会话顺序写入消息，并保证 request_id 幂等。"""
    pool = _pool_or_error()
    if role not in {"user", "assistant", "system"}:
        raise ValueError("不支持的消息类型")
    if not request_id:
        request_id = uuid.uuid4().hex

    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                session = await connection.fetchrow(
                    "SELECT session_id, title FROM chat_sessions WHERE session_id = $1 FOR UPDATE",
                    uuid.UUID(session_id),
                )
                if not session:
                    raise ValueError("会话不存在")

                next_title = session["title"]
                if role == "user" and next_title == "新的旅行对话":
                    next_title = content.strip()[:32] or next_title

                row = await connection.fetchrow(
                    """
                    INSERT INTO chat_messages
                        (session_id, user_id, role, content, request_id, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    RETURNING *
                    """,
                    uuid.UUID(session_id),
                    user_id,
                    role,
                    content,
                    request_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                )
                await connection.execute(
                    "UPDATE chat_sessions SET updated_at = NOW(), title = $2 WHERE session_id = $1",
                    uuid.UUID(session_id),
                    next_title,
                )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise DuplicateRequestError("请求已处理，请勿重复提交") from exc

    return _row_to_message(row)


async def get_recent_messages(session_id: str, limit: int = MEMORY_MESSAGE_LIMIT) -> List[Dict[str, Any]]:
    """按时间正序获取最近消息。"""
    pool = _pool_or_error()
    rows = await pool.fetch(
        """
        SELECT * FROM (
            SELECT * FROM chat_messages
            WHERE session_id = $1
            ORDER BY id DESC
            LIMIT $2
        ) recent_messages
        ORDER BY id ASC
        """,
        uuid.UUID(session_id),
        max(1, limit),
    )
    return [_row_to_message(row) for row in rows]


async def get_messages(session_id: str, limit: int = 100, before_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """分页读取消息，before_id 用于向上翻页。"""
    pool = _pool_or_error()
    if before_id:
        rows = await pool.fetch(
            """
            SELECT * FROM chat_messages
            WHERE session_id = $1 AND id < $2
            ORDER BY id DESC
            LIMIT $3
            """,
            uuid.UUID(session_id),
            before_id,
            max(1, limit),
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM chat_messages
            WHERE session_id = $1
            ORDER BY id DESC
            LIMIT $2
            """,
            uuid.UUID(session_id),
            max(1, limit),
        )
    return [_row_to_message(row) for row in reversed(rows)]


async def count_messages(session_id: str) -> int:
    pool = _pool_or_error()
    return int(await pool.fetchval(
        "SELECT COUNT(*) FROM chat_messages WHERE session_id = $1",
        uuid.UUID(session_id),
    ))


async def list_sessions(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    pool = _pool_or_error()
    rows = await pool.fetch(
        """
        SELECT s.*, COUNT(m.id) AS message_count
        FROM chat_sessions s
        LEFT JOIN chat_messages m ON m.session_id = s.session_id
        WHERE s.user_id = $1
        GROUP BY s.session_id
        ORDER BY s.updated_at DESC
        LIMIT $2
        """,
        user_id,
        max(1, limit),
    )
    result = []
    for row in rows:
        item = _row_to_session(row)
        item["message_count"] = row["message_count"]
        result.append(item)
    return result


def _merge_json(previous: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """合并 JSON；旧值优先，LLM 只填充空字段，列表做去重合并。"""
    merged = dict(previous or {})
    for key, value in (incoming or {}).items():
        if value is None or value == "" or value == []:
            continue
        old_value = merged.get(key)
        if isinstance(old_value, dict) and isinstance(value, dict):
            merged[key] = _merge_json(old_value, value)
        elif isinstance(old_value, list) and isinstance(value, list):
            merged[key] = list(dict.fromkeys([*old_value, *value]))
        elif old_value in (None, "", []):
            merged[key] = value
    return merged


def _extract_json_object(text: str) -> Dict[str, Any]:
    """从模型输出中提取 JSON 对象。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 输出中没有 JSON 对象")
    return json.loads(text[start:end + 1])


def _missing_fields(brief: Dict[str, Any]) -> List[str]:
    labels = {
        "destination": "目的地",
        "dates": "出行日期",
        "travelers": "同行人数",
        "budget": "预算范围",
        "transportation": "交通方式",
        "accommodation": "住宿偏好",
    }
    return [label for field, label in labels.items() if not brief.get(field)]


async def get_trip_brief(session_id: str) -> Dict[str, Any]:
    """获取会话对应的结构化旅行简报。"""
    pool = _pool_or_error()
    row = await pool.fetchrow(
        "SELECT brief, version, updated_at FROM trip_briefs WHERE session_id = $1",
        uuid.UUID(session_id),
    )
    if not row:
        return {"brief": {}, "version": 0}
    return {
        "brief": row["brief"] or {},
        "version": row["version"],
        "updated_at": row["updated_at"].isoformat(),
    }


async def save_trip_brief(
    session_id: str,
    incoming_brief: Dict[str, Any],
    source_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """按“旧值优先、空值不覆盖”的规则保存行程简报。"""
    pool = _pool_or_error()
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                "SELECT brief, version FROM trip_briefs WHERE session_id = $1 FOR UPDATE",
                uuid.UUID(session_id),
            )
            current_brief = row["brief"] or {} if row else {}
            next_version = (row["version"] + 1) if row else 1
            merged = _merge_json(current_brief, incoming_brief)
            result = await connection.fetchrow(
                """
                INSERT INTO trip_briefs (session_id, brief, version, source_message_id)
                VALUES ($1, $2::jsonb, $3, $4)
                ON CONFLICT (session_id) DO UPDATE SET
                    brief = EXCLUDED.brief,
                    version = EXCLUDED.version,
                    source_message_id = EXCLUDED.source_message_id,
                    updated_at = NOW()
                RETURNING brief, version, updated_at
                """,
                uuid.UUID(session_id),
                json.dumps(merged, ensure_ascii=False),
                next_version,
                source_message_id,
            )
    return {
        "brief": result["brief"] or {},
        "version": result["version"],
        "updated_at": result["updated_at"].isoformat(),
    }


async def get_session_summary(session_id: str) -> Dict[str, Any]:
    """获取会话压缩摘要。"""
    pool = _pool_or_error()
    row = await pool.fetchrow(
        """
        SELECT summary, last_message_id, version, updated_at
        FROM session_summaries
        WHERE session_id = $1
        """,
        uuid.UUID(session_id),
    )
    if not row:
        return {"summary": "", "last_message_id": 0, "version": 0}
    return {
        "summary": row["summary"],
        "last_message_id": row["last_message_id"],
        "version": row["version"],
        "updated_at": row["updated_at"].isoformat(),
    }


def _redis_or_error() -> redis.Redis:
    if _redis is None:
        raise RuntimeError("memory store not initialized")
    return _redis


async def process_turn_memory(session_id: str, user_id: str) -> Dict[str, Any]:
    """后台抽取长期记忆、行程简报和补充问题。"""
    lock_key = f"chat:session:{session_id}:memory"
    token = uuid.uuid4().hex
    acquired = await _redis_or_error().set(lock_key, token, nx=True, px=300_000)
    if not acquired:
        return {"processed": False, "reason": "memory_processing_busy"}

    try:
        recent = await get_recent_messages(session_id, limit=6)
        if not recent:
            return {"processed": False, "reason": "no_messages"}

        try:
            await _update_summary(session_id)
        except Exception as exc:
            logger.warning("Update session summary failed: %s", exc)

        current_brief = (await get_trip_brief(session_id))["brief"]
        transcript = "\n".join(f"{item['role']}: {item['content']}" for item in recent)
        user_prompt = (
            f"{_MEMORY_EXTRACTION_PROMPT}\n\n当前行程简报：\n"
            f"{json.dumps(current_brief, ensure_ascii=False)}\n\n最近对话：\n{transcript}"
        )
        agent = ChatAgent(system_prompt="你是旅行信息结构化抽取器。")
        raw_reply = await asyncio.wait_for(
            agent.chat([{"role": "user", "content": user_prompt}]),
            timeout=60,
        )
        extracted = _extract_json_object(raw_reply)

        saved_brief = None
        if isinstance(extracted.get("trip_brief"), dict):
            saved_brief = await save_trip_brief(
                session_id,
                extracted["trip_brief"],
                source_message_id=recent[-1]["id"],
            )

        memories = []
        for raw_memory in extracted.get("memories", [])[:10]:
            content = str(raw_memory.get("content", "")).strip()
            confidence = float(raw_memory.get("confidence", 0.7))
            if len(content) < 4 or confidence < 0.45:
                continue
            item = await remember_fact(
                user_id=user_id,
                content=content,
                memory_type=str(raw_memory.get("memory_type", "trip_fact")),
                confidence=confidence,
                source_session_id=session_id,
                source_message_ids=[recent[-1]["id"]],
            )
            memories.append(item)

        return {
            "processed": True,
            "memory_count": len(memories),
            "memories": memories,
            "trip_brief": saved_brief,
            "next_questions": extracted.get("next_questions", []),
        }
    except Exception as exc:
        logger.warning("Background memory processing failed: %s", exc)
        return {"processed": False, "reason": str(exc)}
    finally:
        try:
            await _redis_or_error().eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)
        except Exception as exc:
            logger.warning("Release memory processing lock failed: %s", exc)


def schedule_memory_processing(session_id: str, user_id: str) -> asyncio.Task:
    """将记忆抽取放入后台，避免阻塞聊天响应。"""
    task = asyncio.create_task(process_turn_memory(session_id, user_id))
    _background_tasks.add(task)

    def _done(done_task: asyncio.Task) -> None:
        _background_tasks.discard(done_task)
        if not done_task.cancelled() and done_task.exception():
            logger.error("Memory processing task error: %s", done_task.exception())

    task.add_done_callback(_done)
    return task


async def build_context(
    user_id: str,
    session_id: Optional[str] = None,
    query: str = "",
) -> Dict[str, Any]:
    """组装一次聊天所需的短期摘要、最近消息、长期记忆和行程简报。"""
    session = await get_or_create_session(user_id=user_id, session_id=session_id)
    current_session_id = session["session_id"]
    summary, memories, recent, trip_brief = await asyncio.gather(
        get_session_summary(current_session_id),
        search_long_term_memories(user_id, query),
        get_recent_messages(current_session_id),
        get_trip_brief(current_session_id),
    )

    memory_context = ""
    if summary.get("summary"):
        memory_context += f"会话摘要：{summary['summary']}\n"
    if memories:
        memory_lines = "\n".join(f"- {item['content']}" for item in memories)
        memory_context += f"长期记忆：\n{memory_lines}\n"
    if trip_brief.get("brief"):
        memory_context += (
            "行程简报："
            f"{json.dumps(trip_brief['brief'], ensure_ascii=False)}\n"
        )

    return {
        "session": session,
        "summary": summary,
        "memories": memories,
        "recent_messages": recent,
        "trip_brief": trip_brief,
        "memory_context": memory_context,
    }


async def _update_summary(session_id: str) -> Optional[Dict[str, Any]]:
    """每 10 条消息触发一次旧摘要压缩。"""
    pool = _pool_or_error()
    total = await count_messages(session_id)
    if total < MEMORY_SUMMARY_EVERY_MESSAGES or total % MEMORY_SUMMARY_EVERY_MESSAGES != 0:
        return None

    current = await get_session_summary(session_id)
    rows = await pool.fetch(
        """
        SELECT id, role, content
        FROM chat_messages
        WHERE session_id = $1 AND id > $2
        ORDER BY id ASC
        LIMIT 30
        """,
        uuid.UUID(session_id),
        current["last_message_id"],
    )
    if not rows:
        return None

    transcript = "\n".join(f"{row['role']}: {row['content']}" for row in rows)
    summary_instruction = (
        "请压缩下面的旅行对话历史，保留目的地、日期、人数、预算、偏好、限制和已确认决定。"
        "只输出不超过 300 字的摘要。\n\n"
        f"旧摘要：{current['summary'] or '无'}\n\n新增对话：\n{transcript}"
    )
    agent = ChatAgent(system_prompt="你是旅行对话记忆压缩器。")
    summary = await asyncio.wait_for(agent.chat([{"role": "user", "content": summary_instruction}]), timeout=45)
    last_message_id = rows[-1]["id"]
    row = await pool.fetchrow(
        """
        INSERT INTO session_summaries (session_id, summary, last_message_id, version)
        VALUES ($1, $2, $3, 1)
        ON CONFLICT (session_id) DO UPDATE SET
            summary = EXCLUDED.summary,
            last_message_id = EXCLUDED.last_message_id,
            version = session_summaries.version + 1,
            updated_at = NOW()
        RETURNING summary, last_message_id, version, updated_at
        """,
        uuid.UUID(session_id),
        summary.strip(),
        last_message_id,
    )
    return {
        "summary": row["summary"],
        "last_message_id": row["last_message_id"],
        "version": row["version"],
        "updated_at": row["updated_at"].isoformat(),
    }


async def get_latest_session(user_id: str) -> Optional[Dict[str, Any]]:
    pool = _pool_or_error()
    row = await pool.fetchrow(
        "SELECT * FROM chat_sessions WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 1",
        user_id,
    )
    return _row_to_session(row) if row else None


async def delete_session(session_id: str, user_id: str) -> bool:
    """删除会话并级联删除消息、摘要和行程简报。"""
    pool = _pool_or_error()
    result = await pool.execute(
        "DELETE FROM chat_sessions WHERE session_id = $1 AND user_id = $2",
        uuid.UUID(session_id),
        user_id,
    )
    return result == "DELETE 1"


def _qdrant_or_error() -> AsyncQdrantClient:
    if _qdrant is None:
        raise RuntimeError("memory store not initialized")
    return _qdrant


def _memory_metadata(
    memory_type: str,
    source_session_id: str,
    source_message_ids: List[int],
) -> Dict[str, Any]:
    return {
        "memory_type": memory_type,
        "source_session_id": source_session_id,
        "source_message_ids": source_message_ids,
        "schema_version": SCHEMA_VERSION,
        "extracted_by": "chat_turn_memory_v1",
    }


def _memory_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "memory_id": item["memory_id"],
        "user_id": item["user_id"],
        "memory_type": item["memory_type"],
        "status": item["status"],
        "content": item["content"],
        "confidence": item["confidence"],
        "content_hash": item["content_hash"],
        "metadata_hash": item["metadata_hash"],
        "source_session_id": item["source_session_id"],
        "source_message_ids": item["source_message_ids"],
        "embedding_model": item["metadata"].get("embedding_model", EMBEDDING_MODEL),
        "embedding_version": item["metadata"].get("embedding_version", 1),
        "schema_version": SCHEMA_VERSION,
        "created_at": item["created_at"],
    }


async def remember_fact(
    user_id: str,
    content: str,
    memory_type: str = "preference",
    confidence: float = 0.8,
    source_session_id: Optional[str] = None,
    source_message_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """写入长期记忆事实；向量索引失败时保留 PG 记录并标记失败。"""
    pool = _pool_or_error()
    clean_content = content.strip()
    if not clean_content:
        raise ValueError("记忆内容不能为空")
    if memory_type not in {"preference", "constraint", "trip_fact"}:
        memory_type = "trip_fact"

    item_hash = content_hash(clean_content)
    message_ids = source_message_ids or []
    item_metadata = _memory_metadata(memory_type, source_session_id or "", message_ids)
    meta_hash = metadata_hash(item_metadata)
    memory_id = uuid.uuid4()

    row = await pool.fetchrow(
        """
        INSERT INTO memory_items
            (memory_id, user_id, content, memory_type, confidence,
             content_hash, metadata_hash, source_session_id,
             source_message_ids, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
        ON CONFLICT (user_id, memory_type, content_hash)
        WHERE status = 'active'
        DO UPDATE SET
            confidence = GREATEST(memory_items.confidence, EXCLUDED.confidence),
            updated_at = NOW()
        RETURNING *
        """,
        memory_id,
        user_id,
        clean_content,
        memory_type,
        confidence,
        item_hash,
        meta_hash,
        uuid.UUID(source_session_id) if source_session_id else None,
        message_ids,
        json.dumps(item_metadata, ensure_ascii=False),
    )
    item = dict(row)
    item["memory_id"] = str(item["memory_id"])
    if item["source_session_id"]:
        item["source_session_id"] = str(item["source_session_id"])

    try:
        vector = await asyncio.to_thread(embed_text, clean_content)
        await _qdrant_or_error().upsert(
            collection_name=QDRANT_COLLECTION,
            points=[
                models.PointStruct(
                    id=uuid.UUID(item["memory_id"]),
                    vector=vector,
                    payload=_memory_payload(item),
                )
            ],
        )
        await pool.execute(
            """
            UPDATE memory_items
            SET embedding_status = 'completed', metadata = metadata || $2::jsonb
            WHERE memory_id = $1
            """,
            uuid.UUID(item["memory_id"]),
            json.dumps({"embedding_model": EMBEDDING_MODEL}, ensure_ascii=False),
        )
        item["embedding_status"] = "completed"
    except Exception as exc:
        logger.warning("Memory embedding failed: %s", exc)
        await pool.execute(
            "UPDATE memory_items SET embedding_status = 'failed' WHERE memory_id = $1",
            uuid.UUID(item["memory_id"]),
        )
        item["embedding_status"] = "failed"

    return item


async def search_long_term_memories(
    user_id: str,
    query: str,
    limit: int = MEMORY_SEARCH_LIMIT,
    score_threshold: float = 0.35,
) -> List[Dict[str, Any]]:
    """基于 Qdrant 检索当前用户的长期记忆。"""
    if not query.strip():
        return []
    vector = await asyncio.to_thread(embed_text, query)
    search_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="user_id",
                match=models.MatchValue(value=user_id),
            ),
            models.FieldCondition(
                key="status",
                match=models.MatchValue(value="active"),
            ),
        ]
    )
    # qdrant-client ≥1.13 的 AsyncQdrantClient 移除了 search()，统一改用 query_points()
    response = await _qdrant_or_error().query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        query_filter=search_filter,
        limit=max(1, limit),
        with_payload=True,
    )
    points = response.points
    result = []
    for point in points:
        payload = point.payload or {}
        if point.score < score_threshold:
            continue
        result.append(
            {
                "memory_id": str(payload.get("memory_id", point.id)),
                "content": payload.get("content", ""),
                "memory_type": payload.get("memory_type", "trip_fact"),
                "confidence": payload.get("confidence", 0.8),
                "score": float(point.score),
                "source_session_id": payload.get("source_session_id"),
                "source_message_ids": payload.get("source_message_ids", []),
            }
        )
    return result
