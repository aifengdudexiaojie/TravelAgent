"""RAG 入库基础设施（PG 真相源 + ES 词法 + Qdrant 向量）
================================================================================
存储架构（设计文档见 docs/rag-architecture-design.md，代码地图见 docs/rag-code-map.md）：

    PostgreSQL    guides 表（完整攻略 JSONB + 身份列）   ← 唯一真相源
    Elasticsearch guide_search_docs（es_info_transfer 的 es_doc，中文 cjk 分词）
    Qdrant        guide_chunks（es_info_transfer 的 vector_content 分块向量，现役）

本模块只提供**基础设施与共享转换**，不含入库编排：
    入库编排见 services/rag/ingest.py；唯一文本转换入口见 services/rag/transform.py。

设计要点
--------
- guide_id 为 UUID：由调用方指定，或按 (destination,title) 生成 deterministic uuid5，
  重复入库自动覆盖（幂等），不会重复插入。
- ES 文档 _id = guide_id，Qdrant payload.guide_id = guide_id → 三库同键。
- ES / Qdrant 均为"可重建的派生索引"；PostgreSQL 是唯一真相源。
- 派生文本（search_content / 各分区文本）只存在于 ES 与 Qdrant，绝不写进 PG。
- PostgreSQL 驱动自动选择：psycopg3 → psycopg2 → asyncpg（三者取其一即可）。

环境变量
--------
    PG_DSN 或 DATABASE_URL    PostgreSQL 连接串
    ES_URL                    Elasticsearch 地址（默认 http://localhost:9200）
    QDRANT_URL / QDRANT_API_KEY
    GUIDE_SEARCH_INDEX        ES 索引名（默认 guide_search_docs）
    RAG_VECTOR_COLLECTIONS    参与检索的 Qdrant 集合，逗号分隔（默认 guide_chunks）
                              （兼容旧名 RAG_EXTRA_COLLECTIONS）

用法（共享能力）
----------------
    from services.rag.store import GuideStores, build_search_doc, guide_id_for

    stores = GuideStores()
    stores.ensure_schema()                                  # 幂等建表/建索引/建集合
    doc = build_search_doc(guide_id, record, keywords)       # ES 文档（复用 es_info_transfer）
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from services.env_bootstrap import env_file_hint, load_env
from services.rag.transform import _clean_text, es_info_transfer

# ⚠ 必须在下面的模块级环境变量读取之前执行，否则 .env 里的
#   DATABASE_URL / ES_URL / QDRANT_URL / 集合名 等配置会被静默忽略
load_env()

# ============================================================
# 配置
# ============================================================
PG_DSN = (
    os.getenv("PG_DSN", "").strip()
    or os.getenv("DATABASE_URL", "").strip()
    or "postgresql://postgres:postgres@localhost:5432/travel_agent"
)
ES_URL = os.getenv("ES_URL", "http://localhost:9200")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip()

GUIDE_INDEX = os.getenv("GUIDE_SEARCH_INDEX", "guide_search_docs")

# 参与检索/需要保证存在的 Qdrant 向量集合（逗号分隔）。
# 现役只有 guide_chunks（es_info_transfer 的分块向量）；
# 旧路径的 guide_facts / guide_semantics 已停用，如需恢复把它加进这个列表即可。
VECTOR_COLLECTIONS = [
    c.strip() for c in (
        os.getenv("RAG_VECTOR_COLLECTIONS")
        or os.getenv("RAG_EXTRA_COLLECTIONS")
        or "guide_chunks"
    ).split(",") if c.strip()
]

DEFAULT_OWNER = {
    "user_id": "rag_test_user",
    "username": "rag_tester",
    "nickname": "RAG测试用户",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# PostgreSQL 层（真相源）
# ============================================================
DDL_GUIDES = """
CREATE TABLE IF NOT EXISTS guides (
    id            UUID PRIMARY KEY,
    user_id       TEXT        NOT NULL,
    username      TEXT        NOT NULL,
    nickname      TEXT        NOT NULL,
    title         TEXT        NOT NULL,
    destination   TEXT        NOT NULL,
    days          INTEGER,
    summary       TEXT,
    content_json  JSONB       NOT NULL,
    keywords      TEXT[]      NOT NULL DEFAULT '{}',
    rating        SMALLINT,
    rating_text   TEXT,
    is_public     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

DDL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_guides_user_id ON guides (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_guides_public  ON guides (is_public)",
    "CREATE INDEX IF NOT EXISTS idx_guides_dest    ON guides (destination)",
)

# 元数据表：记录入库时使用的 embedding 向量空间（查询时比对，防模型不一致）
DDL_META = """
CREATE TABLE IF NOT EXISTS rag_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

META_EMBEDDING_SIGNATURE = "embedding_signature"


def _to_asyncpg_sql(sql: str) -> str:
    """psycopg 风格 %s 占位符 → asyncpg 风格 $n；并补 ANY 的数组类型标注。"""
    counter = {"n": 0}

    def repl(_m):
        counter["n"] += 1
        return f"${counter['n']}"

    out = re.sub(r"%s", repl, sql)
    return re.sub(r"ANY\(\$(\d+)\)", r"ANY($\1::text[])", out)


class _PsycopgSession:
    """psycopg3 / psycopg2 同步会话。"""

    def __init__(self, conn):
        self._conn = conn

    def query(self, sql: str, params=()):
        cur = self._conn.cursor()
        cur.execute(sql, params or ())
        rows = cur.fetchall() if cur.description else []
        cur.close()
        return rows

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


class _AsyncpgSession:
    """把异步驱动 asyncpg 包装成同步接口：后台事件循环 + run_coroutine_threadsafe。

    项目 requirements 已固定 asyncpg，优先复用以避免额外安装 psycopg。
    """

    def __init__(self, dsn: str):
        import asyncio
        import threading

        self._asyncio = asyncio
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop_thread, daemon=True, name="pg-loop").start()
        self._pool = self._submit(self._create_pool(dsn))

    def _loop_thread(self):
        self._asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        return self._asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _create_pool(self, dsn: str):
        import asyncpg
        return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)

    async def _query(self, sql: str, params: tuple):
        stmt = _to_asyncpg_sql(sql)
        head = stmt.lstrip().upper()
        async with self._pool.acquire() as conn:
            if head.startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "VALUES")):
                records = await conn.fetch(stmt, *params)
                return [tuple(r) for r in records]
            await conn.execute(stmt, *params)      # DDL 等无返回语句
            return []

    def query(self, sql: str, params=()):
        return self._submit(self._query(sql, tuple(params or ())))

    def close(self):
        try:
            self._submit(self._pool.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)


def pg_session(dsn: Optional[str] = None):
    """自动选择可用 PG 驱动，返回 (session, driver_name)。"""
    dsn = dsn or PG_DSN
    try:
        import psycopg                                    # psycopg 3
        return _PsycopgSession(psycopg.connect(dsn, autocommit=True)), "psycopg3"
    except ImportError:
        pass
    try:
        import psycopg2                                   # psycopg 2
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return _PsycopgSession(conn), "psycopg2"
    except ImportError:
        pass
    try:
        import asyncpg                                    # 项目已有依赖
        return _AsyncpgSession(dsn), "asyncpg"
    except ImportError as exc:
        raise RuntimeError(
            '缺少 PostgreSQL 驱动，请安装其一：pip install "psycopg[binary]"  或  pip install asyncpg'
        ) from exc


def pg_exec(pg, sql: str, params=None):
    return pg.query(sql, params or ())


def pg_ensure_schema(pg) -> None:
    """幂等建表（含索引与元数据表）。"""
    pg_exec(pg, DDL_GUIDES)
    for stmt in DDL_INDEXES:
        pg_exec(pg, stmt)
    pg_exec(pg, DDL_META)


def pg_set_meta(pg, key: str, value: str) -> None:
    pg_exec(
        pg,
        """INSERT INTO rag_meta (key, value, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (key, value),
    )


def pg_get_meta(pg, key: str) -> Optional[str]:
    rows = pg_exec(pg, "SELECT value FROM rag_meta WHERE key = %s", (key,))
    return rows[0][0] if rows else None


def pg_upsert_guide(pg, guide_id: str, guide: dict, content: dict, keywords: list) -> None:
    """攻略写入 PG（幂等 upsert：同 id 覆盖）。"""
    pg_exec(
        pg,
        """
        INSERT INTO guides (id, user_id, username, nickname, title, destination, days,
                            summary, content_json, keywords, rating, rating_text,
                            is_public, created_at, updated_at)
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, now(), now())
        ON CONFLICT (id) DO UPDATE SET
            title        = EXCLUDED.title,
            destination  = EXCLUDED.destination,
            days         = EXCLUDED.days,
            summary      = EXCLUDED.summary,
            content_json = EXCLUDED.content_json,
            keywords     = EXCLUDED.keywords,
            is_public    = EXCLUDED.is_public,
            updated_at   = now()
        """,
        (
            guide_id,
            guide.get("user_id", DEFAULT_OWNER["user_id"]),
            guide.get("username", DEFAULT_OWNER["username"]),
            guide.get("nickname", DEFAULT_OWNER["nickname"]),
            guide["title"], guide["destination"], guide.get("days"),
            guide.get("summary", ""),
            json.dumps(content, ensure_ascii=False), keywords,
            guide.get("rating"), guide.get("rating_text"),
            bool(guide.get("is_public", False)),
        ),
    )


def pg_delete_guide(pg, guide_id: str) -> None:
    pg_exec(pg, "DELETE FROM guides WHERE id::text = %s", (guide_id,))


def pg_fetch_guides(pg, ids: Iterable[str]) -> dict:
    """按 id 批量取回（返回 {id: row_dict}；调用方自行按融合顺序重排）。"""
    ids = list(ids)
    if not ids:
        return {}
    rows = pg_exec(
        pg,
        """SELECT id, user_id, title, destination, days, summary, content_json, keywords,
                  is_public, created_at
           FROM guides WHERE id::text = ANY(%s)""",
        (ids,),
    )
    out = {}
    for r in rows:
        out[str(r[0])] = {
            "guide_id": str(r[0]), "user_id": r[1], "title": r[2], "destination": r[3],
            "days": r[4], "summary": r[5],
            "content": r[6] if isinstance(r[6], dict) else json.loads(r[6]),
            "keywords": list(r[7] or []), "is_public": r[8],
            "created_at": r[9].isoformat() if hasattr(r[9], "isoformat") else str(r[9]),
        }
    return out


# ============================================================
# Elasticsearch 层（词法/过滤，无向量）
# ============================================================
ES_MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "guide_id": {"type": "keyword"},
            "user_id": {"type": "keyword"},
            "is_public": {"type": "boolean"},
            # cjk 分析器：中文按 bigram 切分（"西"+"安"→"西安"），
            # 避免 standard 分析器逐字切分导致"的/是/去/上"等虚词贡献高分（实测三亚曾因此虚高）
            "title": {"type": "text", "analyzer": "cjk",
                      "fields": {"keyword": {"type": "keyword"}}},
            "destination": {"type": "keyword",
                            "fields": {"text": {"type": "text", "analyzer": "cjk"}}},
            "summary": {"type": "text", "analyzer": "cjk"},
            "overview": {"type": "text", "analyzer": "cjk"},
            "daily_plan": {"type": "text", "analyzer": "cjk"},
            "spots_catalog": {"type": "text", "analyzer": "cjk"},
            "food_catalog": {"type": "text", "analyzer": "cjk"},
            "extra_recommendations": {"type": "text", "analyzer": "cjk"},
            "precautions_summary": {"type": "text", "analyzer": "cjk"},
            "budget_breakdown": {"type": "text", "analyzer": "cjk"},
            "trade_off_summary": {"type": "text", "analyzer": "cjk"},
            "daily_plan_text": {"type": "text", "analyzer": "cjk"},
            "search_content": {"type": "text", "analyzer": "cjk"},
            "days": {"type": "integer"},
            "keywords": {"type": "keyword"},
            "updated_at": {"type": "date"},
        }
    },
}


def es_apply_mapping(client, index: Optional[str] = None) -> List[str]:
    """给**已存在**的索引补充缺失字段映射（幂等；ES 允许新增字段，不允许改动已存在字段）。

    为什么必须要有：索引是早期版本建的，只声明了 title/summary/... 等少量字段。
    之后 es_info_transfer 新增了 search_content / daily_plan / spots_catalog 等字段，
    如果不补映射，这些字段会被 ES **动态映射**（默认 standard 分析器 → 中文逐字切分），
    于是"改了 ES_MAPPING"看起来生效、实际索引里仍是错的。

    返回本次新增的字段名；已存在但我们期望不同 analyzer 的字段会打印告警（需重建索引）。
    """
    index = index or GUIDE_INDEX
    if not client.indices.exists(index=index):
        return []
    current = (client.indices.get_mapping(index=index)
               .get(index, {}).get("mappings", {}).get("properties", {}))
    missing = {k: v for k, v in ES_MAPPING["mappings"]["properties"].items()
               if k not in current}
    if missing:
        client.indices.put_mapping(index=index, properties=missing)

    # 检查已存在字段的分析器是否与期望一致（不一致只能重建索引）
    mismatched = []
    for name in missing.keys() | set(current.keys()):
        want = ES_MAPPING["mappings"]["properties"].get(name, {}).get("analyzer")
        got = current.get(name, {}).get("analyzer")
        if want and got and want != got:
            mismatched.append(f"{name}: 索引={got} 期望={want}")
    if mismatched:
        print("   ⚠ 以下字段分析器与期望不一致，需 --reset-derived 重建索引："
              + "；".join(mismatched))
    return sorted(missing)


def es_client():
    from services.es_client import get_es
    return get_es()


def es_ensure_index(client, reset: bool = False) -> None:
    exists = client.indices.exists(index=GUIDE_INDEX)
    if exists and reset:
        client.indices.delete(index=GUIDE_INDEX)
        exists = False
    if not exists:
        # 现代关键字写法（elasticsearch 9.x 仍兼容 body= 但已废弃）
        client.indices.create(
            index=GUIDE_INDEX,
            settings=ES_MAPPING["settings"],
            mappings=ES_MAPPING["mappings"],
        )


def build_search_doc(guide_id: str, guide: dict, keywords: Optional[list] = None) -> dict:
    """构建 ES 检索文档：**直接复用 services.rag.transform.es_info_transfer 的 es_doc**。

    这样 ingest_by_es_transfer.py 等入库脚本写出的 ES 文档 **完全同构**
    避免早期"一条路径写数组、另一条写拼接字符串"的形状不一致问题。

    仅覆盖身份类字段（PG 为准），其余字段一律沿用 es_info_transfer 的输出。
    """
    # guide 可能是外层 wrapper（含 content），也可能是 content 本身
    content = guide.get("content") or guide
    transfer = es_info_transfer(content)
    es_doc = dict(transfer.get("es_doc") or {})

    days = guide.get("days")
    if days is None:
        days = es_doc.get("days")

    es_doc.update({
        "guide_id": guide_id,
        "user_id": guide.get("user_id", DEFAULT_OWNER["user_id"]),
        "is_public": bool(guide.get("is_public", False)),
        "title": guide.get("title") or es_doc.get("title", ""),
        "destination": guide.get("destination") or es_doc.get("destination", ""),
        "days": days,
        "summary": guide.get("summary") or es_doc.get("summary", ""),
        "keywords": keywords or es_doc.get("keywords") or [],
        "updated_at": _now_iso(),
    })
    return {k: v for k, v in es_doc.items() if v is not None}


def es_index_doc(client, guide_id: str, doc: dict) -> None:
    client.index(index=GUIDE_INDEX, id=guide_id, document=doc, refresh="wait_for")


def es_delete_doc(client, guide_id: str) -> None:
    try:
        client.delete(index=GUIDE_INDEX, id=guide_id, refresh="wait_for")
    except Exception:
        pass


# ============================================================
# Qdrant 层（双通道向量）
# ============================================================
def qdrant_client():
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise RuntimeError("缺少 qdrant-client，请安装：pip install qdrant-client") from exc
    kwargs = {"url": QDRANT_URL}
    if QDRANT_API_KEY:
        kwargs["api_key"] = QDRANT_API_KEY
    return QdrantClient(**kwargs)


def _collection_exists(client, name: str) -> bool:
    try:
        client.get_collection(name)
        return True
    except Exception:
        return False


def qdrant_ensure_collections(client, dim: int, collections: Optional[List[str]] = None,
                             reset: bool = False) -> None:
    """确保指定集合存在且维度一致（默认 = VECTOR_COLLECTIONS，即 RAG_VECTOR_COLLECTIONS 配置）。"""
    from qdrant_client.models import Distance, VectorParams

    names = collections or VECTOR_COLLECTIONS
    for name in names:
        if _collection_exists(client, name):
            if reset:
                client.delete_collection(name)
            else:
                info = client.get_collection(name)
                existing = info.config.params.vectors.size  # type: ignore[attr-defined]
                if existing != dim:
                    raise RuntimeError(
                        f"集合 {name} 维度 {existing} 与当前 embedding 维度 {dim} 不一致；"
                        f"请用 reset 重建（会清空该集合），或把 EMBEDDING_DIM 调成 {existing}"
                    )
                continue
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )


def qdrant_upsert(client, collection: str, point_id: str, vector: list, payload: dict) -> None:
    from qdrant_client.models import PointStruct
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        wait=True,
    )


def qdrant_delete_guide(client, collection: str, guide_id: str) -> None:
    """按 payload.guide_id 删除某攻略在指定集合中的全部向量点。"""
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    if not _collection_exists(client, collection):
        return
    client.delete(
        collection_name=collection,
        points_selector=Filter(must=[
            FieldCondition(key="guide_id", match=MatchValue(value=guide_id))]),
        wait=True,
    )


def guide_id_for(guide: dict) -> str:
    """deterministic guide_id：同 (destination,title) 重复入库会覆盖而非新增。

    若需每次生成新记录（例如同一标题的多份攻略），请显式传入 guide["guide_id"]。
    """
    if guide.get("guide_id"):
        return str(guide["guide_id"])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"guide:{guide['destination']}:{guide['title']}"))


# ============================================================
# 服务连通性预检（避免只看到底层驱动的晦涩报错）
# ============================================================
def _host_port(url: str, default_port: int) -> tuple:
    from urllib.parse import urlparse
    u = urlparse(url)
    return (u.hostname or "localhost"), (u.port or default_port)


def _tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pg_roundtrip(dsn: str) -> tuple:
    """真实跑一次 SELECT 1，确认账号/库/权限都正常。"""
    try:
        import asyncio

        import asyncpg

        async def run():
            conn = await asyncpg.connect(dsn=dsn, timeout=4)
            try:
                return await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

        return True, f"SELECT 1 -> {asyncio.run(run())}"
    except ImportError:
        pass
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    for mod_name in ("psycopg", "psycopg2"):
        try:
            mod = __import__(mod_name)
            conn = mod.connect(dsn)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            val = cur.fetchone()[0]
            conn.close()
            return True, f"SELECT 1 -> {val}"
        except ImportError:
            continue
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return True, "端口可达（无可用驱动做真实查询）"


def check_pg(dsn: Optional[str] = None) -> dict:
    dsn = dsn or PG_DSN
    host, port = _host_port(dsn, 5432)
    if not _tcp_open(host, port):
        return {"service": "PostgreSQL", "target": f"{host}:{port}", "ok": False,
                "detail": "端口未监听（服务未启动或地址/端口不对）",
                "hint": "启动 PG：docker compose up -d postgres，或启动本机 PostgreSQL 服务"
                        "（Windows: Get-Service postgresql*）；并检查 .env 的 DATABASE_URL"}
    ok, detail = _pg_roundtrip(dsn)
    if ok:
        return {"service": "PostgreSQL", "target": f"{host}:{port}", "ok": True,
                "detail": detail, "hint": ""}
    if "middle of operation" in detail or "ConnectionDoesNotExist" in detail:
        detail += "   ← asyncpg 把「认证失败/被服务端拒绝」也报成这句话"
    return {"service": "PostgreSQL", "target": f"{host}:{port}", "ok": False,
            "detail": detail,
            "hint": "端口可达但握手失败，常见原因：① DATABASE_URL 里的密码不对（PG 服务端日志会记录 "
                    "'Password 认证失败'）② 目标库不存在 ③ pg_hba.conf 未放行。"
                    "PG 日志：<数据目录>/log/postgresql-*.log"}


def check_es(url: Optional[str] = None) -> dict:
    url = url or ES_URL
    host, port = _host_port(url, 9200)
    if not _tcp_open(host, port):
        return {"service": "Elasticsearch", "target": f"{host}:{port}", "ok": False,
                "detail": "端口未监听（服务未启动）",
                "hint": r"启动原生 ES：E:\elasticsearch-9.5.3\bin\elasticsearch.bat"
                        r"   或容器版：docker compose --profile full up -d elasticsearch"}
    try:
        from services.es_client import es_health
        health = es_health()
        ok = health.get("status") == "ok"
        detail = (f"version={health.get('version')}" if ok
                  else str(health.get("message", "?"))[:100])
        hint = "" if ok else (health.get("hint") or "ES 可达但 info() 失败：检查安全/认证配置")
        return {"service": "Elasticsearch", "target": f"{host}:{port}", "ok": ok,
                "detail": detail, "hint": hint}
    except Exception as exc:
        return {"service": "Elasticsearch", "target": f"{host}:{port}", "ok": False,
                "detail": f"{type(exc).__name__}: {exc}", "hint": "检查 elasticsearch 客户端版本是否匹配"}


def check_qdrant(url: Optional[str] = None) -> dict:
    url = url or QDRANT_URL
    host, port = _host_port(url, 6333)
    if not _tcp_open(host, port):
        return {"service": "Qdrant", "target": f"{host}:{port}", "ok": False,
                "detail": "端口未监听（服务未启动）",
                "hint": "启动：docker compose up -d qdrant    （或修正 .env 的 QDRANT_URL）"}
    try:
        client = qdrant_client()
        cols = client.get_collections().collections
        return {"service": "Qdrant", "target": f"{host}:{port}", "ok": True,
                "detail": f"现有集合 {len(cols)} 个", "hint": ""}
    except Exception as exc:
        return {"service": "Qdrant", "target": f"{host}:{port}", "ok": False,
                "detail": f"{type(exc).__name__}: {exc}", "hint": "检查 qdrant-client 版本与 api_key"}


def preflight_services(quiet: bool = False) -> tuple:
    """独立探测三个依赖服务，返回 (all_ok, results)。避免"先连 PG 报晦涩错"的体验。"""
    results = [check_pg(), check_es(), check_qdrant()]
    if not quiet:
        print("── 依赖服务连通性 ──────────────────────────────────────")
        for r in results:
            mark = "✅" if r["ok"] else "❌"
            print(f"  {mark} {r['service']:<14} {r['target']:<22} {r['detail']}")
            if not r["ok"] and r.get("hint"):
                print(f"       └─ {r['hint']}")
        print("────────────────────────────────────────────────────────")
    return all(r["ok"] for r in results), results


# ============================================================
# 统一入口
# ============================================================
class GuideStores:
    """三库连接集合：PG（真相源）+ ES（词法）+ Qdrant（向量）。"""

    def __init__(self, embedding_dim: Optional[int] = None):
        from services.embedding import EMBEDDING_DIM, describe
        # 维度解析优先级：显式入参 > .env 的 EMBEDDING_DIM > 默认 1024
        self.embedding_dim = int(embedding_dim or EMBEDDING_DIM)
        self.embedding_info = describe()
        try:
            self.pg, self.pg_driver = pg_session()
        except Exception as exc:
            raise RuntimeError(
                f"PostgreSQL 连接失败：{exc}\n"
                f"  DSN：{PG_DSN}\n"
                f"  提示：PG 未启动时 asyncpg 只会报 'connection was closed in the middle of operation'。\n"
                f"  排查：python -c \"from services.rag.store import preflight_services; preflight_services()\"\n"
                f"  启动：docker compose up -d postgres"
            ) from exc
        self.es = es_client()
        self.qd = qdrant_client()

    def ensure_schema(self, reset_derived: bool = False) -> None:
        """幂等建表 + 建/重建索引与集合（reset_derived=True 会清空派生索引）。"""
        pg_ensure_schema(self.pg)
        es_ensure_index(self.es, reset=reset_derived)
        qdrant_ensure_collections(self.qd, self.embedding_dim,
                                  collections=VECTOR_COLLECTIONS, reset=reset_derived)

    def close(self) -> None:
        """关闭三库连接（PG 驱动各异，统一走 session.close）。"""
        self.pg.close()


def config_report(embedding_dim: Optional[int] = None) -> dict:
    """当前三库 + embedding 配置来源清单（不建立任何连接，便于打印排查）。"""
    from services.embedding import describe

    info = describe()
    return {
        "env_file": env_file_hint(),
        "pg_dsn": PG_DSN,
        "pg_dsn_from": "PG_DSN" if os.getenv("PG_DSN", "").strip()
                        else ("DATABASE_URL" if os.getenv("DATABASE_URL", "").strip() else "默认值"),
        "es_url": ES_URL,
        "qdrant_url": QDRANT_URL,
        "guide_index": GUIDE_INDEX,
        "collections": VECTOR_COLLECTIONS,
        "embedding_dim": int(embedding_dim or info["dim"]),
        "embedding": info,
    }


def preflight_embedding(expected_dim: Optional[int] = None) -> tuple:
    """向量化连通性预检：真实调用一次 embedding，校验维度与配置一致。

    返回 (ok: bool, actual_dim: int|None, message: str)。
    在正式入库前调用可避免"写到一半才发现 Key/模型/维度不对"。
    """
    from services.embedding import EMBEDDING_DIM, embed_text

    want = int(expected_dim or EMBEDDING_DIM)
    try:
        vec = embed_text("向量化预检")
    except Exception as exc:
        return False, None, f"embedding 调用失败：{exc}（检查 DASHSCOPE_API_KEY / EMBEDDING_BASE_URL）"
    got = len(vec)
    if got != want:
        return False, got, (
            f"维度不一致：API 返回 {got} 维，而配置/集合为 {want} 维。"
            f"请统一 EMBEDDING_DIM={got} 并用 reset 重建 Qdrant 集合"
        )
    return True, got, f"embedding 正常，维度 {got}"


def storage_stats(stores: GuideStores) -> dict:
    """三库当前数据量统计（用于验证/运维巡检）。集合列表动态取自 Qdrant 实际状态。"""
    (pg_count,) = pg_exec(stores.pg, "SELECT count(*) FROM guides")[0]
    try:
        es_count = stores.es.count(index=GUIDE_INDEX)["count"]
    except Exception:
        es_count = None

    # 动态列出 Qdrant 里所有集合的点数（不再硬编码旧集合名）
    points = {}
    try:
        for c in stores.qd.get_collections().collections:
            try:
                points[c.name] = stores.qd.count(collection_name=c.name).count
            except Exception:
                points[c.name] = None
    except Exception:
        points = {}

    # 入库登记 vs 当前配置的向量空间比对
    try:
        from services.embedding import embedding_signature
        stored_sig = pg_get_meta(stores.pg, META_EMBEDDING_SIGNATURE)
        current_sig = embedding_signature()
    except Exception:
        stored_sig, current_sig = None, None

    stats = {
        "pg_guides": pg_count,
        "es_docs": es_count,
        "embedding": stores.embedding_info,
        "embedding_signature_ingested": stored_sig or "（未登记，重新入库一次即可登记）",
        "embedding_signature_current": current_sig,
        "embedding_consistent": (stored_sig == current_sig) if stored_sig else None,
    }
    for name, n in sorted(points.items()):
        stats[f"qdrant_{name}"] = n
    return stats
