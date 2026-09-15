"""RAG 入库编排（函数化，供 CLI 与其它程序调用）
================================================================================
流程（唯一转换入口 = services/rag/transform.es_info_transfer）：

    攻略 JSON
      ├─ PostgreSQL   pg_upsert_truth()   只存真相源（content_json + 身份列）
      ├─ Elasticsearch build_search_doc() → es_index_doc()（es_doc + 自动补字段映射）
      └─ Qdrant       typed_chunks() → upsert_chunks()（vector_content 分块向量）

所有原先的命令行参数都保留为 IngestOptions 字段，CLI 只是它的薄封装：

    from services.rag.ingest import IngestOptions, ingest_guides, stats

    options = IngestOptions(reset_derived=True, verbose=True)
    result = ingest_guides(options=options)                 # 缺省用内置示例数据
    result = ingest_guides(load_guides("example.json"), options=options)
    print(stats(options))
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.env_bootstrap import load_env

load_env()

from services.embedding import EMBEDDING_DIM, embed_texts          # noqa: E402
from services.rag.store import (                                  # noqa: E402
    GUIDE_INDEX,
    META_EMBEDDING_SIGNATURE,
    build_search_doc,
    check_es,
    check_pg,
    check_qdrant,
    es_apply_mapping,
    es_client,
    es_ensure_index,
    es_index_doc,
    guide_id_for,
    pg_ensure_schema,
    pg_exec,
    pg_session,
    pg_set_meta,
    qdrant_client,
    qdrant_upsert,
)
from services.rag.transform import es_info_transfer               # noqa: E402

# 本路径的向量集合（可用环境变量或 options.collection 覆盖）
CHUNK_COLLECTION = os.getenv("QDRANT_CHUNKS_COLLECTION", "guide_chunks")

DEFAULT_OWNER = {
    "user_id": os.getenv("INGEST_USER_ID", "rag_test_user"),
    "username": os.getenv("INGEST_USERNAME", "rag_tester"),
    "nickname": os.getenv("INGEST_NICKNAME", "RAG 测试用户"),
}


# ============================================================
# 参数（对应原 CLI 的全部选项）
# ============================================================
@dataclass
class IngestOptions:
    # 单篇覆盖项（原 --title/--destination/--days/--summary）
    title: Optional[str] = None
    destination: Optional[str] = None
    days: Optional[int] = None
    summary: Optional[str] = None
    # 归属与元数据（原 --user-id/--username/--nickname/--trust）
    user_id: Optional[str] = None
    username: Optional[str] = None
    nickname: Optional[str] = None
    trust: Any = None
    # 存储控制（原 --collection/--no-es/--no-qdrant/--reset-derived）
    collection: str = CHUNK_COLLECTION
    no_es: bool = False
    no_qdrant: bool = False
    reset_derived: bool = False
    # 运行模式（原 --dry-run/--stats/--skip-service-check）
    dry_run: bool = False
    stats_only: bool = False
    skip_service_check: bool = False
    verbose: bool = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 读取与归一化
# ============================================================
def load_guides(path: Optional[str] = None) -> List[dict]:
    """读取待入库攻略：给 path 读 JSON 文件；不给则用内置示例数据（SEED_GUIDES）。"""
    if not path:
        from seed_rag_test_data import SEED_GUIDES
        return [dict(item) for item in SEED_GUIDES]

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        raise ValueError("输入 JSON 必须是对象，或由对象组成的数组")
    return data


def split_record(item: dict) -> Tuple[dict, dict]:
    """兼容两种输入形态：外层记录（含 content） / 原始 travel-summarizer JSON。"""
    if isinstance(item.get("content"), dict):
        return dict(item), dict(item["content"])
    return dict(item), dict(item)


def parse_trust(value: Any) -> Any:
    """信任度/来源等元数据：允许 JSON 字符串、数字或纯文本。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def normalize_record(item: dict, options: Optional[IngestOptions] = None) -> dict:
    """提取**身份类**字段（这些才进 PG）；内容细节全部交给 es_info_transfer。"""
    options = options or IngestOptions()
    record, content = split_record(item)
    content = record.get("content") or content
    meta = content.get("meta") or {}
    destinations = meta.get("destinations") or []

    destination = (
        options.destination
        or record.get("destination")
        or content.get("destination")
        or (destinations[0] if isinstance(destinations, list) and destinations else destinations)
    )
    if not destination:
        raise ValueError("缺少 destination：请在 JSON 中提供，或通过 options.destination 指定")

    days = options.days if options.days is not None else record.get("days")
    if days is None:
        days = meta.get("total_days")
    if isinstance(days, str):
        days = days.replace("天", "").strip() or None
    try:
        days = int(days) if days is not None else None
    except (TypeError, ValueError):
        days = None

    title = (
        options.title
        or record.get("title")
        or content.get("title")
        or meta.get("title")
        or f"{destination}{days or ''}天旅游攻略"
    )

    trust = options.trust if options.trust is not None else record.get("trust")

    return {
        "guide_id": record.get("guide_id") or item.get("guide_id"),
        "user_id": options.user_id or record.get("user_id") or DEFAULT_OWNER["user_id"],
        "username": options.username or record.get("username") or DEFAULT_OWNER["username"],
        "nickname": options.nickname or record.get("nickname") or DEFAULT_OWNER["nickname"],
        "title": title,
        "destination": destination,
        "days": days,
        "summary": options.summary or record.get("summary") or "",
        "is_public": bool(record.get("is_public", False)),
        "rating": record.get("rating"),
        "rating_text": record.get("rating_text"),
        "trust": trust,
        "tags": record.get("tags") or [],
        "content": content,
    }


# ============================================================
# 分块定型
# ============================================================
def typed_chunks(transfer: dict) -> List[dict]:
    """把 es_info_transfer 的 vector_content（扁平字符串数组）映射成带类型的块。

    类型由 es_doc 里"同一份文本"反查得到：
    meta / trade_off / day_plan / spot / food / extra / precautions / budget。
    """
    es_doc = transfer.get("es_doc") or {}
    kind_of: Dict[str, str] = {}

    def mark(text: Any, kind: str) -> None:
        t = (text or "").strip()
        if t and t not in kind_of:
            kind_of[t] = kind

    mark(es_doc.get("summary"), "meta")
    mark(es_doc.get("trade_off_summary"), "trade_off")
    for t in es_doc.get("daily_plan") or []:
        mark(t, "day_plan")
    for t in es_doc.get("spots_catalog") or []:
        mark(t, "spot")
    for t in es_doc.get("food_catalog") or []:
        mark(t, "food")
    for t in es_doc.get("extra_recommendations") or []:
        mark(t, "extra")
    mark(es_doc.get("precautions_summary"), "precautions")
    mark(es_doc.get("budget_breakdown"), "budget")

    chunks: List[dict] = []
    for i, raw in enumerate(transfer.get("vector_content") or []):
        text = (raw or "").strip()
        if not text:
            continue
        chunks.append({"chunk_index": i, "chunk_type": kind_of.get(text, "chunk"), "text": text})
    return chunks


def merged_keywords(record: dict, transfer: dict) -> List[str]:
    """关键词 = 目的地 + es_info_transfer 产出的关键词 + 标签（去重保序）。"""
    kws = [record["destination"]]
    kws += [str(k) for k in ((transfer.get("es_doc") or {}).get("keywords") or [])]
    kws += [str(t) for t in (record.get("tags") or [])]
    return list(dict.fromkeys(k for k in kws if k))


# ============================================================
# PostgreSQL（真相源）
# ============================================================
def pg_ensure_truth_schema(pg: Any) -> None:
    """建表（沿用 store 的表结构）+ 本路径需要的 trust 元数据列。"""
    pg_ensure_schema(pg)
    pg_exec(pg, "ALTER TABLE guides ADD COLUMN IF NOT EXISTS trust JSONB")


def pg_upsert_truth(pg: Any, guide_id: str, record: dict) -> None:
    """把攻略 JSON 与身份字段写入 PG。

    ⚠ 刻意**不写**任何派生字段（keywords/search_content/各 catalog 文本…）：
       PG 是真相源，派生文本随时可由 content_json 重新生成，只应存在于 ES / Qdrant。
    """
    trust = record.get("trust")
    pg_exec(
        pg,
        """
        INSERT INTO guides (
            id, user_id, username, nickname, title, destination, days, summary,
            content_json, rating, rating_text, is_public, trust, created_at, updated_at
        )
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s, %s::jsonb, now(), now())
        ON CONFLICT (id) DO UPDATE SET
            user_id = EXCLUDED.user_id, username = EXCLUDED.username,
            nickname = EXCLUDED.nickname, title = EXCLUDED.title,
            destination = EXCLUDED.destination, days = EXCLUDED.days,
            summary = EXCLUDED.summary, content_json = EXCLUDED.content_json,
            rating = EXCLUDED.rating, rating_text = EXCLUDED.rating_text,
            is_public = EXCLUDED.is_public, trust = EXCLUDED.trust,
            updated_at = now()
        """,
        (
            guide_id,
            record["user_id"], record["username"], record["nickname"],
            record["title"], record["destination"], record["days"], record["summary"],
            json.dumps(record["content"], ensure_ascii=False),
            record.get("rating"), record.get("rating_text"), record["is_public"],
            None if trust is None else json.dumps(trust, ensure_ascii=False),
        ),
    )


# ============================================================
# Elasticsearch（es_doc）
# ============================================================
def es_prepare(client: Any, reset: bool = False) -> List[str]:
    """确保索引存在并补齐缺失字段映射，返回本次新增的字段名。"""
    es_ensure_index(client, reset=reset)
    return es_apply_mapping(client)


# ============================================================
# Qdrant（分块向量）
# ============================================================
def qdrant_prepare(qd: Any, collection: str, dim: int, reset: bool = False) -> None:
    from qdrant_client.models import Distance, VectorParams

    exists = True
    try:
        info = qd.get_collection(collection)
    except Exception:
        exists = False

    if exists and reset:
        qd.delete_collection(collection)
        exists = False
    if not exists:
        qd.create_collection(collection_name=collection,
                             vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        return

    existing_dim = info.config.params.vectors.size  # type: ignore[attr-defined]
    if existing_dim != dim:
        raise RuntimeError(
            f"集合 {collection} 维度 {existing_dim} ≠ 当前 embedding 维度 {dim}；"
            f"请用 reset_derived=True 重建，或统一 EMBEDDING_DIM")


def qdrant_delete_guide(qd: Any, collection: str, guide_id: str) -> None:
    """删除该攻略在指定集合中的旧分块（幂等：避免攻略变短后残留脏数据）。"""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    try:
        qd.delete(
            collection_name=collection,
            points_selector=Filter(must=[
                FieldCondition(key="guide_id", match=MatchValue(value=guide_id))]),
            wait=True,
        )
    except Exception:
        pass


def upsert_chunks(qd: Any, collection: str, guide_id: str,
                  record: dict, transfer: dict) -> int:
    """vector_content 分批向量化后写入 Qdrant；返回写入块数。"""
    chunks = typed_chunks(transfer)
    if not chunks:
        return 0

    qdrant_delete_guide(qd, collection, guide_id)
    vectors = embed_texts([c["text"] for c in chunks])

    base = dict(transfer.get("qdrant_payload") or {})
    base.update({
        "guide_id": guide_id,
        "user_id": record["user_id"],
        "is_public": record["is_public"],
        "title": record["title"],
        "destination": record["destination"],
        "days": record["days"],
        "channel": "chunks",
    })

    for chunk, vector in zip(chunks, vectors):
        payload = {
            **base,
            "section": chunk["chunk_type"],
            "chunk_type": chunk["chunk_type"],
            "chunk_index": chunk["chunk_index"],
            "text": chunk["text"],
        }
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"{guide_id}:chunk:{chunk['chunk_index']}"))
        qdrant_upsert(qd, collection, point_id, vector, payload)
    return len(chunks)


# ============================================================
# 预览与统计
# ============================================================
def build_preview(record: dict, transfer: dict, guide_id: str) -> dict:
    """转换结果预览（不连库）。"""
    es_doc = transfer.get("es_doc") or {}
    chunks = typed_chunks(transfer)
    kinds: Dict[str, int] = {}
    for c in chunks:
        kinds[c["chunk_type"]] = kinds.get(c["chunk_type"], 0) + 1
    return {
        "guide_id": guide_id,
        "title": record["title"],
        "destination": record["destination"],
        "days": record["days"],
        "trust": record.get("trust"),
        "es_doc_fields": sorted(es_doc.keys()),
        "es_doc_keywords": es_doc.get("keywords"),
        "es_doc_search_content_len": len(es_doc.get("search_content") or ""),
        "vector_chunks": len(chunks),
        "chunk_types": kinds,
        "qdrant_payload": transfer.get("qdrant_payload"),
        "search_content_preview": (es_doc.get("search_content") or "")[:200],
    }


def stats(options: Optional[IngestOptions] = None, stores: Any = None) -> dict:
    """三库数据量统计（可按需只连不写）。"""
    options = options or IngestOptions()
    own = stores is None
    if own:
        pg, _ = pg_session()
        es = None if options.no_es else es_client()
        qd = None if options.no_qdrant else qdrant_client()
    else:
        pg, es, qd = stores.pg, stores.es, stores.qd

    out: Dict[str, Any] = {}
    try:
        out["pg_guides"] = pg_exec(pg, "SELECT count(*) FROM guides")[0][0]
    except Exception as exc:
        out["pg_guides"] = f"读取失败：{exc}"
    if es is not None:
        try:
            out["es_docs"] = es.count(index=GUIDE_INDEX)["count"]
        except Exception as exc:
            out["es_docs"] = f"读取失败：{exc}"
    if qd is not None:
        try:
            out[f"qdrant_{options.collection}"] = qd.count(
                collection_name=options.collection).count
        except Exception as exc:
            out[f"qdrant_{options.collection}"] = f"读取失败：{exc}"

    if own:
        pg.close()
    return out


# ============================================================
# 主流程（原 CLI 的 main 逻辑）
# ============================================================
def ingest_guides(items: Optional[List[dict]] = None,
                  options: Optional[IngestOptions] = None,
                  stores: Any = None) -> dict:
    """批量入库（原 ingest_by_es_transfer.py 的 main 流程）。

    参数
        items   待入库攻略列表；None 表示使用内置示例数据（SEED_GUIDES）
        options 全部原命令行选项（见 IngestOptions）
        stores  可选的已连接存储（复用连接时传入；None 则内部建立）

    返回
        {"ingested": [ {guide_id, destination, title, chunks} ],
         "previews": [...]（dry_run 时）, "stats": {...}, "dry_run": bool}
    """
    options = options or IngestOptions()
    options.collection = options.collection or CHUNK_COLLECTION

    # ① 读取 + 转换（唯一入口 es_info_transfer）
    records = [normalize_record(item, options)
               for item in (items if items is not None else load_guides())]
    transforms = []
    for record in records:
        guide_id = guide_id_for(record)
        transfer = es_info_transfer(record["content"])
        transforms.append((record, transfer, guide_id))

    # ② 预览模式：不连库
    if options.dry_run:
        previews = [build_preview(r, t, g) for r, t, g in transforms]
        if options.verbose:
            for p in previews:
                print(json.dumps(p, ensure_ascii=False, indent=2))
        return {"ingested": [], "previews": previews, "stats": {}, "dry_run": True}

    # ③ 依赖预检
    if not options.skip_service_check:
        checks = [check_pg()]
        if not options.no_es:
            checks.append(check_es())
        if not options.no_qdrant:
            checks.append(check_qdrant())
        failed = [c for c in checks if not c["ok"]]
        if failed:
            for r in failed:
                msg = f"❌ {r['service']}  {r['detail']}"
                if r.get("hint"):
                    msg += f"\n   └─ {r['hint']}"
                print(msg)
            raise RuntimeError("依赖服务未就绪：" + "、".join(c["service"] for c in failed))

    # ④ 连接与建 schema
    own = stores is None
    if own:
        pg, driver = pg_session()
        if options.verbose:
            print(f"PG 驱动：{driver}")
        es = None if options.no_es else es_client()
        qd = None if options.no_qdrant else qdrant_client()
    else:
        pg, es, qd = stores.pg, stores.es, stores.qd

    pg_ensure_truth_schema(pg)
    if es is not None:
        added = es_prepare(es, reset=options.reset_derived)
        if options.verbose:
            print(f"ES 索引 {GUIDE_INDEX}：{'重建完成' if options.reset_derived else '就绪'}"
                  + (f"，新增字段映射 {added}" if added else ""))
    if qd is not None:
        qdrant_prepare(qd, options.collection, EMBEDDING_DIM, reset=options.reset_derived)
        if options.verbose:
            print(f"Qdrant 集合 {options.collection}：就绪（dim={EMBEDDING_DIM}）")

    # ⑤ 纯统计模式
    if options.stats_only:
        out = stats(options, stores=None if own else stores)
        if options.verbose:
            for k, v in out.items():
                print(f"  {k:<28}: {v}")
        if own:
            pg.close()
        return {"ingested": [], "previews": [], "stats": out, "dry_run": False}

    # ⑥ 逐篇写入
    ingested: List[dict] = []
    try:
        for record, transfer, guide_id in transforms:
            pg_upsert_truth(pg, guide_id, record)
            if es is not None:
                es_index_doc(es, guide_id,
                             build_search_doc(guide_id, record,
                                              merged_keywords(record, transfer)))
            chunks = (upsert_chunks(qd, options.collection, guide_id, record, transfer)
                      if qd is not None else 0)
            ingested.append({"guide_id": guide_id,
                             "destination": record["destination"],
                             "title": record["title"], "chunks": chunks})
            if options.verbose:
                print(f"✔ [{record['destination']}] {record['title']}")
                print(f"   PG(真相源) 已写入 | ES "
                      f"{'已索引' if es is not None else '跳过'} | Qdrant {chunks} 块")

        if qd is not None:
            from services.embedding import embedding_signature
            pg_set_meta(pg, META_EMBEDDING_SIGNATURE, embedding_signature())

        out_stats = stats(options, stores=None if own else stores)
        if options.verbose:
            print()
            for k, v in out_stats.items():
                print(f"  {k:<28}: {v}")
            print(f"\n完成：{len(ingested)} 篇攻略，"
                  f"Qdrant 共写入 {sum(i['chunks'] for i in ingested)} 个向量块")
    finally:
        if own:
            pg.close()

    return {"ingested": ingested, "previews": [], "stats": out_stats, "dry_run": False}


def ingest_from_file(path: str, options: Optional[IngestOptions] = None) -> dict:
    """便捷入口：从 JSON 文件批量入库。"""
    return ingest_guides(load_guides(path), options=options)


def delete_guide(guide_id: str, options: Optional[IngestOptions] = None,
                 stores: Any = None) -> None:
    """三库联动删除：PG 行 + ES 文档 + Qdrant 分块。"""
    from services.rag.store import es_delete_doc

    options = options or IngestOptions()
    own = stores is None
    if own:
        pg, _ = pg_session()
        es = None if options.no_es else es_client()
        qd = None if options.no_qdrant else qdrant_client()
    else:
        pg, es, qd = stores.pg, stores.es, stores.qd

    pg_exec(pg, "DELETE FROM guides WHERE id::text = %s", (guide_id,))
    if es is not None:
        es_delete_doc(es, guide_id)
    if qd is not None:
        qdrant_delete_guide(qd, options.collection, guide_id)
    if own:
        pg.close()
