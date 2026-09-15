"""RAG 检索（读路径）
================================================================================
查询流程：

    用户 query
      ├─ Elasticsearch  词法检索（es_doc 字段：title^3 / summary^2 / destination^2 /
      │                 keywords / search_content^2.5 / spots_catalog 等，cjk 中文分词）
      └─ Qdrant         向量检索：对**每个配置的集合**（VECTOR_COLLECTIONS，
                        默认 guide_chunks = es_info_transfer 的分块向量）各召回一路
            ↓ RRF 融合（通道内按攻略去重、保留通道归因）
            ↓ 交叉编码重排序（services/rerank.py）→ final_score
      ↓
    按 guide_id 从 PostgreSQL 取回完整攻略（真相源）→ 组装 RAG 上下文

对外接口：
    search(query, stores=None, top_k=5)          -> {"hits": [...], "channel_counts": {...}}
    build_context(hits, max_chars=3000)          -> str（喂给 LLM 的参考上下文）
    verify(stores=None)                          -> (all_passed, results)  内置回归探针

环境变量：RAG_RRF_K、RAG_W_ES、RAG_W_<集合名>、RAG_VECTOR_COLLECTIONS、
          RAG_USE_RERANK、RAG_RERANK_CANDIDATES、RAG_W_RRF、RAG_W_RERANK
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional

from services.env_bootstrap import load_env

load_env()

from services.rag.store import (      # noqa: E402  (需在 load_env 之后导入)
    GUIDE_INDEX,
    META_EMBEDDING_SIGNATURE,
    VECTOR_COLLECTIONS,
    GuideStores,
    pg_fetch_guides,
    pg_get_meta,
)

RRF_K = int(os.getenv("RAG_RRF_K", "60"))

logger = logging.getLogger("services.rag.retrieval")

# 通道权重：es = ES 词法；其余按 Qdrant 集合名取权重（未列出的集合默认 1.0）。
# 保留 guide_facts / guide_semantics 的旧环境变量，便于临时把旧集合加回 VECTOR_COLLECTIONS。
SOURCE_WEIGHTS = {
    "es": float(os.getenv("RAG_W_ES", "1.0")),
    "guide_facts": float(os.getenv("RAG_W_FACTS", "1.0")),
    "guide_semantics": float(os.getenv("RAG_W_SEMANTICS", "1.0")),
}


# ============================================================
# 归属 / 可见性过滤（谁能检索到谁的攻略）
# ============================================================
VISIBILITIES = ("all", "own", "public", "own_or_public")


def resolve_visibility(user_id: Optional[str] = None, public_only: bool = False,
                       visibility: Optional[str] = None) -> str:
    """把 (user_id, public_only, visibility) 归一成一个可见性模式。

         all            不过滤（CLI / 回归探针的默认行为）
         own            只看该用户自己的攻略（聊天"历史模式"的默认语义）
         public         只看已公开的攻略
         own_or_public  自己的 + 别人公开的

    兼容旧调用：public_only=True → public；只传 user_id → own；都不传 → all。
    """
    if visibility:
        mode = visibility
    elif public_only:
        mode = "public"
    elif user_id:
        mode = "own"
    else:
        mode = "all"

    if mode not in VISIBILITIES:
        raise ValueError(f"未知 visibility={mode!r}，可选：{VISIBILITIES}")

    if mode in ("own", "own_or_public") and not user_id:
        # 没有 user_id 就无法按归属过滤：退化为 public（只暴露已公开攻略），
        # 绝不退化成 all —— 否则会把别人的私有攻略检索出来。
        logger.warning("visibility=%s 但未提供 user_id，退化为 public（仅公开攻略）", mode)
        mode = "public"
    return mode


def row_visible(row: dict, visibility: str, user_id: Optional[str]) -> bool:
    """PG 行（真相源）是否满足可见性要求 —— 最后一道兜底过滤。"""
    if visibility == "all":
        return True
    if visibility == "public":
        return bool(row.get("is_public"))
    if visibility == "own":
        return row.get("user_id") == user_id
    if visibility == "own_or_public":
        return row.get("user_id") == user_id or bool(row.get("is_public"))
    return True


def _es_owner_filter(visibility: str, user_id: Optional[str]) -> list:
    """ES bool.filter 的归属过滤子句（all 时返回空列表）。"""
    if visibility == "own":
        return [{"term": {"user_id": user_id}}]
    if visibility == "public":
        return [{"term": {"is_public": True}}]
    if visibility == "own_or_public":
        return [{"bool": {
            "should": [{"term": {"user_id": user_id}}, {"term": {"is_public": True}}],
            "minimum_should_match": 1,
        }}]
    return []


def _qdrant_owner_filter(visibility: str, user_id: Optional[str]):
    """Qdrant payload 过滤（chunk payload 里带了 user_id / is_public）。"""
    if visibility == "all":
        return None
    from qdrant_client import models

    if visibility == "own":
        return models.Filter(must=[models.FieldCondition(
            key="user_id", match=models.MatchValue(value=user_id))])
    if visibility == "public":
        return models.Filter(must=[models.FieldCondition(
            key="is_public", match=models.MatchValue(value=True))])
    # own_or_public：should = 任一命中（自己的 or 公开的）
    return models.Filter(should=[
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
        models.FieldCondition(key="is_public", match=models.MatchValue(value=True)),
    ])


# ============================================================
# 三路召回
# ============================================================
def es_lexical_search(client, query: str, top_k: int = 10,
                      user_id: Optional[str] = None,
                      public_only: bool = False,
                      visibility: Optional[str] = None) -> List[dict]:
    """ES 词法检索（多字段加权 + 归属过滤 + 命中片段高亮）。

    visibility 见 resolve_visibility()：own 只召回该用户自己的攻略，
    避免把别人的私有攻略（例如测试写入的数据）检索进聊天上下文。

    返回按 _score 降序的命中列表；每条含 `highlights`（命中的原文片段），
    便于排查"为什么这篇排第一"。
    """
    mode = resolve_visibility(user_id, public_only, visibility)

    es_query = {
        "bool": {
            "must": [{
                "multi_match": {
                    "query": query,
                    "fields": [
                        "title^3",
                        "search_content^2.5",
                        "summary^2",
                        "overview^2",
                        "destination^2",
                        "keywords",
                        "spots_catalog^1.5",
                        "food_catalog^1.5",
                        "daily_plan",
                        "extra_recommendations",
                        "precautions_summary",
                        "budget_breakdown",
                    ],
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                }
            }],
            # 归属过滤走 filter：不参与打分，且是硬性约束
            "filter": _es_owner_filter(mode, user_id),
        }
    }
    res = client.search(
        index=GUIDE_INDEX,
        query=es_query,
        size=top_k,
        highlight={
            "fields": {
                "title": {}, "summary": {}, "search_content": {},
                "spots_catalog": {}, "food_catalog": {}, "daily_plan": {},
            },
            "fragment_size": 60,
            "number_of_fragments": 1,
            "pre_tags": ["«"], "post_tags": ["»"],
        },
    )

    hits = []
    for h in res.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        frags = []
        for field, snippets in (h.get("highlight") or {}).items():
            for s in snippets:
                frags.append(f"{field}: {s}")
        hits.append({
            "guide_id": src.get("guide_id", h["_id"]),
            "score": h.get("_score") or 0.0,
            "source": "es",
            "title": src.get("title", ""),
            "destination": src.get("destination", ""),
            "highlights": frags[:2],
        })
    return hits


def qdrant_search(client, collection: str, vector: List[float],
                  top_k: int = 10, source: str = "",
                  user_id: Optional[str] = None,
                  public_only: bool = False,
                  visibility: Optional[str] = None) -> List[dict]:
    """Qdrant 向量检索（qdrant-client 1.10+ 用 query_points，老版本回退 search）。

    归属过滤在服务端做：chunk payload 里带了 user_id / is_public（见 transform.py
    的 qdrant_payload），所以向量通道也能只召回"自己的（或公开的）"攻略。
    """
    mode = resolve_visibility(user_id, public_only, visibility)
    query_filter = _qdrant_owner_filter(mode, user_id)

    if hasattr(client, "query_points"):
        res = client.query_points(collection_name=collection, query=vector,
                                  limit=top_k, with_payload=True,
                                  query_filter=query_filter)
        points = res.points
    else:  # qdrant-client < 1.10
        points = client.search(collection_name=collection, query_vector=vector,
                               limit=top_k, with_payload=True,
                               query_filter=query_filter)

    hits = []
    for p in points:
        payload = p.payload or {}
        hits.append({
            "guide_id": payload.get("guide_id", ""),
            "score": float(p.score),
            "source": source or payload.get("channel", collection),
            "section": payload.get("section"),
            "title": payload.get("title", ""),
            "destination": payload.get("destination", ""),
            "text": payload.get("text", ""),
        })
    return hits


# ============================================================
# 融合：RRF（Reciprocal Rank Fusion）
# ============================================================
def rrf_fuse(hit_lists: List[List[dict]], top_k: int = 5,
             dedupe_per_channel: bool = True) -> List[dict]:
    """score = Σ_source w_source / (K + rank)；同分以最佳单路排名稳定排序。

    dedupe_per_channel=True：同一通道内同一篇攻略只按其**最佳排名**计一次分。
    必要性：通道B 每篇攻略有多个语义段（住宿/节奏/文化/…），若逐段累加，
    该通道权重会被隐性放大 N 倍——实测中"杭州+乌镇"正是靠 4 段挤掉了更相关的西安。
    """
    scores, attribution, best_rank = {}, {}, {}
    for hits in hit_lists:
        seen_in_channel = set()
        for rank, h in enumerate(hits, start=1):
            gid = h.get("guide_id")
            if not gid:
                continue
            if dedupe_per_channel and gid in seen_in_channel:
                continue                      # 只取该通道内首次（最佳）出现
            seen_in_channel.add(gid)
            w = SOURCE_WEIGHTS.get(h["source"], 1.0)
            scores[gid] = scores.get(gid, 0.0) + w / (RRF_K + rank)
            tag = f"{h['source']}#{rank}"
            if h.get("section"):
                tag += f"({h['section']})"
            attribution.setdefault(gid, []).append(tag)
            best_rank[gid] = min(best_rank.get(gid, rank), rank)

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], best_rank[kv[0]]))[:top_k]
    return [{"guide_id": gid, "rrf_score": sc, "channels": attribution[gid]}
            for gid, sc in ordered]


# ============================================================
# 向量空间一致性校验（防"入库/查询模型不一致"）
# ============================================================
def check_embedding_consistency(stores: GuideStores, strict: bool = False) -> dict:
    """比对「入库时登记的 embedding 签名」与「当前查询用的签名」。

    为什么需要：embed_text() 是门面函数，若误开 USE_LOCAL_EMBEDDING 或改了
    EMBEDDING_MODEL，查询向量会来自另一个模型 —— 维度可能相同、Qdrant 不报错，
    但相似度全错，检索质量静默崩坏。这里显式拦住这种情况。

    返回 {"stored": str|None, "current": str, "consistent": bool|None, "message": str}
    """
    from services.embedding import embedding_signature

    current = embedding_signature()
    try:
        stored = pg_get_meta(stores.pg, META_EMBEDDING_SIGNATURE)
    except Exception as exc:
        # 读路径不做 DDL：库里还没有 rag_meta 表（新库/未重新入库）时按"未登记"处理
        return {"stored": None, "current": current, "consistent": None,
                "message": f"向量空间未登记：无法读取 rag_meta（{exc}）。"
                           f"重跑一次 ingest_by_es_transfer.py 即可登记（当前查询用 {current}）。"}

    if stored is None:
        msg = (f"向量空间未登记：库中未记录入库时使用的 embedding（当前查询用 {current}）。"
               f"建议重跑一次 ingest_by_es_transfer.py 完成登记，以免模型不一致无感知。")
        return {"stored": None, "current": current, "consistent": None, "message": msg}

    if stored != current:
        msg = (f"向量空间不一致！入库用的是 [{stored}]，当前查询用的是 [{current}]。"
               f"此时检索结果不可信：请把 .env 改回入库时的模型配置，"
               f"或用 --reset-derived 重新入库后再查。")
        if strict:
            raise RuntimeError(msg)
        return {"stored": stored, "current": current, "consistent": False, "message": msg}

    return {"stored": stored, "current": current, "consistent": True, "message": ""}


# ============================================================
# 主查询入口
# ============================================================
def search(query: str, stores: Optional[GuideStores] = None,
           top_k: int = 5, per_source: int = 10,
           user_id: Optional[str] = None, public_only: bool = False,
           visibility: Optional[str] = None,
           strict_embedding: bool = False,
           use_rerank: Optional[bool] = None,
           rerank_candidates: Optional[int] = None,
           debug_ranking: Optional[bool] = None,
           verbose: bool = False) -> dict:
    """三库融合检索 → （可选）交叉编码重排序 → PG 回填全文。

    参数 visibility：谁能被检索到（详见 resolve_visibility()）。
      · 不传时按用户_id/public_only 推断：只传 user_id → own；都不传 → all；
      · 聊天"历史模式"传 own，确保只检索当前用户自己的攻略。

    参数 debug_ranking：是否打印各通道召回排序明细（默认取环境变量
    RAG_DEBUG_RANKING，缺省 true）。

    返回：
        {
          "query": str,
          "hits": [ {guide_id,title,destination,days,summary,content,keywords,
                     rrf_score, rerank_score, final_score, rerank_reason, channels} ... ],
          "channel_counts": {"es": n, "guide_chunks": n},
          "vector_ok": bool,
          "embedding": {...},          # 向量空间一致性
          "rerank": {...},             # 重排序后端信息
          "visibility": str,           # 实际生效的可见性模式
          "warnings": [str],
        }
    """
    own_stores = stores is None
    stores = stores or GuideStores()
    warnings: List[str] = []

    mode = resolve_visibility(user_id, public_only, visibility)

    # ⓪ 向量空间一致性（入库模型 vs 查询模型）
    consistency = check_embedding_consistency(stores, strict=strict_embedding)
    if consistency["message"]:
        warnings.append(consistency["message"])

    # ① query 向量化（失败则退化为纯词法检索）
    vec = None
    try:
        from services.embedding import embed_text
        vec = embed_text(query)
    except Exception as exc:
        warnings.append(f"query 向量化失败，退化为纯 ES 词法检索：{exc}")

    # ② 多路召回：ES 词法 + 每个配置的 Qdrant 集合各自作为一个独立通道
    #    有归属过滤时向量通道要多召回一些（过滤是服务端做的，但候选池仍需留足余量）
    vector_top_k = per_source if mode == "all" else max(per_source * 3, top_k * 5)
    rankings: dict = {}
    es_hits = es_lexical_search(stores.es, query, top_k=per_source,
                                user_id=user_id, public_only=public_only,
                                visibility=mode)
    rankings["es"] = es_hits

    vector_hit_lists: List[List[dict]] = []
    channel_counts = {"es": len(es_hits)}
    for coll in VECTOR_COLLECTIONS:
        hits: List[dict] = []
        if vec:
            try:
                hits = qdrant_search(stores.qd, coll, vec, top_k=vector_top_k,
                                     source=coll, user_id=user_id,
                                     public_only=public_only, visibility=mode)
            except Exception as exc:
                # 集合不存在 / 维度不符 / Qdrant 异常：只记录告警，不让整个检索失败
                warnings.append(f"向量集合 {coll} 检索失败：{type(exc).__name__}: {exc}")
        rankings[coll] = hits
        channel_counts[coll] = len(hits)
        vector_hit_lists.append(hits)
    vector_hits = [h for lst in vector_hit_lists for h in lst]

    # ③ 重排序开关与候选池大小
    if use_rerank is None:
        use_rerank = os.getenv("RAG_USE_RERANK", "true").lower() in ("1", "true", "yes", "on")
    pool = int(rerank_candidates or os.getenv("RAG_RERANK_CANDIDATES", "0") or 0)
    pool = max(pool, top_k * 2, 8) if use_rerank else top_k

    # ④ RRF 融合（通道内按攻略去重，避免"多段"通道隐性加权）→ 取候选池
    fused = rrf_fuse([es_hits] + vector_hit_lists, top_k=pool)

    # ⑤ 回 PostgreSQL 取全文（真相源），并按可见性做最后一道兜底过滤
    #    （ES / Qdrant 已在各自查询里过滤，这里防的是 payload 缺失或索引不一致）
    rows = pg_fetch_guides(stores.pg, [f["guide_id"] for f in fused])
    merged: List[dict] = []
    filtered_out = 0
    for f in fused:
        row = rows.get(f["guide_id"])
        if not row:
            warnings.append(f"guide_id={f['guide_id'][:8]}… 在 PG 中不存在（派生索引与真相源不一致，建议重建索引）")
            continue
        if not row_visible(row, mode, user_id):
            filtered_out += 1
            continue
        merged.append({**row, "rrf_score": f["rrf_score"], "channels": f["channels"]})

    if mode != "all" and not merged:
        scope = {"own": "你自己的攻略", "public": "已公开的攻略",
                 "own_or_public": "你自己的或他人公开的攻略"}.get(mode, mode)
        warnings.append(f"可见性过滤（{mode}）后没有可用攻略：只检索「{scope}」范围")

    # ⑥ 交叉编码重排序：为每个候选算 rerank_score（是否真的回答了 query）
    rerank_meta = {"enabled": bool(use_rerank), "provider": None, "model": "",
                   "error": "", "scored": 0, "candidates": len(merged)}
    if use_rerank and merged:
        try:
            # 注意：不要写 `from services.rag import rerank as rerank_mod` 或
            # `import services.rag.rerank as rerank_mod` —— 包 __init__ 重导出了同名
            # 函数 rerank()，两种写法都会绑定到那个函数而不是子模块，
            # 后续 .provider_info() 会报 "'function' object has no attribute 'provider_info'"。
            from services.rag.rerank import provider_info as rerank_provider_info
            from services.rag.rerank import rerank as score_with_rerank

            info = rerank_provider_info()
            rerank_meta.update({"provider": info["provider"], "model": info["model"]})
            if info["enabled"]:
                docs = [build_candidate_text(g, vector_hits)
                        for g in merged]
                ranked = score_with_rerank(query, docs)
                by_index = {r["index"]: r for r in ranked}
                for i, g in enumerate(merged):
                    r = by_index.get(i)
                    g["rerank_score"] = float(r["score"]) if r else 0.0
                    g["rerank_reason"] = r.get("reason", "") if r else ""
                rerank_meta["scored"] = len(ranked)
            else:
                rerank_meta["error"] = "未启用（RERANK_PROVIDER=none 且无可用 Key）"
        except Exception as exc:
            rerank_meta["error"] = str(exc)
            warnings.append(f"重排序失败，退回 RRF 顺序：{exc}")

    # ⑦ 新排序指标：final = w_rrf * 归一化RRF + w_rerank * 归一化rerank
    rrf_order = [(g.get("destination", ""), g["rrf_score"]) for g in merged]  # 重排前的 RRF 顺序快照
    hits = finalize_ranking(merged, top_k=top_k)

    if own_stores:
        stores.close()

    # ⑧ 打印各通道召回排序明细（默认开启，可用 RAG_DEBUG_RANKING=false 或参数关闭）
    if debug_ranking is None:
        debug_ranking = os.getenv("RAG_DEBUG_RANKING", "true").lower() in ("1", "true", "yes", "on")
    if debug_ranking:
        print_channel_rankings(query, rankings, rrf_order, hits)

    if verbose:
        print(f"  各通道召回：{channel_counts}"
              f"  重排序={'开' if rerank_meta['enabled'] else '关'}")

    return {
        "query": query,
        "hits": hits,
        "channel_counts": channel_counts,
        "channel_rankings": rankings,       # 各通道原始排序明细（ES / 每个向量集合）
        "rrf_order": rrf_order,             # 重排序前的 RRF 顺序
        "vector_ok": vec is not None,
        "visibility": mode,                 # 实际生效的可见性（own / public / own_or_public / all）
        "owner": {"user_id": user_id, "filtered_out": filtered_out},
        "embedding": {"stored": consistency["stored"], "current": consistency["current"],
                      "consistent": consistency["consistent"]},
        "rerank": rerank_meta,
        "warnings": warnings,
    }


def print_channel_rankings(query: str, rankings: dict, rrf_order: List[tuple],
                           hits: List[dict], snippet: int = 46,
                           show_top: int = 10) -> None:
    """打印一次查询的排序明细：各通道召回排序 + RRF 顺序 + 重排后的最终顺序。

    - ES 通道带命中片段（«…» 标记命中词），便于看清"为什么它排第一"
    - 向量通道带 chunk 类型与文本片段
    """
    print("\n" + "─" * 76)
    print(f"召回排序（query：{query}）")
    print("─" * 76)

    for channel, hits_of_channel in rankings.items():
        label = "ES 词法" if channel == "es" else f"Qdrant·{channel}"
        print(f"[{label}] 命中 {len(hits_of_channel)} 条")
        if not hits_of_channel:
            print("    （无命中）")
            continue
        for i, h in enumerate(hits_of_channel[:show_top], 1):
            head = f"    #{i:<2} score={h.get('score', 0):>8.4f}  [{h.get('destination', '')}] {h.get('title', '')[:28]}"
            print(head)
            extra = ""
            if h.get("section") or h.get("chunk_type"):
                extra = f"chunk={h.get('section') or h.get('chunk_type')}"
            if h.get("highlights"):
                extra += ("  " if extra else "") + str(h["highlights"][0])[:snippet + 20]
            elif h.get("text"):
                extra += ("  " if extra else "") + f"文本：{h['text'][:snippet]}…"
            if extra:
                print(f"          {extra}")

    print("─" * 76)
    if rrf_order:
        print("RRF 融合顺序：" + " > ".join(
            f"{d}({s:.5f})" for d, s in rrf_order[:show_top]))
    if hits:
        print("重排后最终顺序：" + " > ".join(
            f"{h.get('destination', '')}(final={h.get('final_score', 0):.4f}"
            + (f",rerank={h['rerank_score']:.4f}" if h.get("rerank_score") is not None else "")
            + ")"
            for h in hits))

        # 分数构成：解释"为什么名次会变"（RRF 只看排名、量程窄；rerank 看内容、主导最终分）
        parts0 = hits[0].get("score_parts") or {}
        if parts0:
            w_rrf, w_rr = parts0.get("w_rrf", 0.0), parts0.get("w_rerank", 0.0)
            if w_rr > 0:
                print(f"\n分数构成（final = {w_rrf:g}·归一化RRF + {w_rr:g}·归一化rerank，"
                      f"各自除以本批最大值后再加权）：")
                for h in hits:
                    p = h.get("score_parts") or {}
                    print(f"   {h.get('destination', ''):<10}"
                          f"rrf_norm={p.get('rrf_norm', 0):.3f} → {p.get('rrf_part', 0):.3f}"
                          f"   rerank_norm={p.get('rerank_norm') or 0:.3f} → {p.get('rerank_part', 0):.3f}"
                          f"   合计={h.get('final_score', 0):.4f}")
            else:
                print("\n（重排序未生效：final = 归一化 RRF）")
    print("─" * 76)


def build_candidate_text(guide: dict, evidence_hits: List[dict], max_chars: int = 1500) -> str:
    """为重排序构造候选文本。

    顺序很关键（曾被这个坑咬过）：**命中的证据片段放最前**，其次才是摘要。
    证据片段 = 各向量通道命中的分块文本（guide_chunks 的 chunk_type：
    meta/trade_off/day_plan/spot/food/extra/precautions/budget），按相似度降序取前若干条。
    早期版本把证据放在末尾又限制长度，导致含《长恨歌》的片段被截断，
    交叉编码器因此把最相关的攻略判为无关——务必保持"证据优先"。
    """
    parts = [f"{guide.get('title', '')}｜{guide.get('destination', '')}"
             f"{guide.get('days', '')}天"]

    picked, seen = [], set()
    for h in sorted(evidence_hits, key=lambda x: -(x.get("score") or 0.0)):
        if h.get("guide_id") != guide.get("guide_id"):
            continue
        t = (h.get("text") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        kind = h.get("section") or h.get("chunk_type") or h.get("source") or ""
        picked.append(f"[{kind}] {t}")
        if len(picked) >= 5:
            break
    if picked:
        parts.append("命中证据：" + " ".join(picked))

    if guide.get("summary"):
        parts.append(guide["summary"])

    if guide.get("keywords"):
        parts.append("关键词：" + "、".join(list(guide["keywords"])[:8]))

    return "\n".join(parts)[:max_chars]


def finalize_ranking(merged: List[dict], top_k: int = 5) -> List[dict]:
    """计算最终排序指标并截断。

    final_score = W_RRF * (rrf/最大rrf) + W_RERANK * (rerank/最大rerank)
    - 两条得分量纲不同，先各自归一化再加权，权重可用环境变量调节
    - 若重排序未生效（无 rerank_score），则退化为纯 RRF 排序
    - 每个候选都会写入 `score_parts`（归一化分量与权重），便于解释名次变化
    """
    if not merged:
        return []
    w_rrf = float(os.getenv("RAG_W_RRF", "0.4"))
    w_rerank = float(os.getenv("RAG_W_RERANK", "0.6"))

    max_rrf = max((g.get("rrf_score") or 0.0) for g in merged) or 1.0
    scored = [g for g in merged if g.get("rerank_score") is not None]
    max_rr = max((g.get("rerank_score") or 0.0) for g in scored) if scored else 0.0

    for g in merged:
        rrf_norm = (g.get("rrf_score") or 0.0) / max_rrf
        g["rrf_norm"] = rrf_norm
        if not scored or max_rr <= 0:
            g["final_score"] = rrf_norm
            g["score_parts"] = {"w_rrf": 1.0, "w_rerank": 0.0,
                                "rrf_norm": rrf_norm, "rerank_norm": None,
                                "rrf_part": rrf_norm, "rerank_part": 0.0}
        else:
            rr_norm = (g.get("rerank_score") or 0.0) / max_rr
            g["rerank_norm"] = rr_norm
            g["final_score"] = w_rrf * rrf_norm + w_rerank * rr_norm
            g["score_parts"] = {"w_rrf": w_rrf, "w_rerank": w_rerank,
                                "rrf_norm": rrf_norm, "rerank_norm": rr_norm,
                                "rrf_part": w_rrf * rrf_norm,
                                "rerank_part": w_rerank * rr_norm}

    merged.sort(key=lambda g: (g.get("final_score") or 0.0,
                               g.get("rerank_score") or 0.0,
                               g.get("rrf_score") or 0.0), reverse=True)
    return merged[:top_k]


def search_ids(query: str, stores: Optional[GuideStores] = None, top_k: int = 5) -> List[str]:
    """只取融合后的 guide_id 列表（供上层按需自行取数）。"""
    return [h["guide_id"] for h in search(query, stores=stores, top_k=top_k)["hits"]]


# ============================================================
# RAG 上下文组装（喂给 LLM）
# ============================================================
def build_context(hits: Iterable[dict], max_chars: int = 3000,
                  include_spots: bool = True,
                  owner_user_id: Optional[str] = None,
                  header: Optional[str] = None) -> str:
    """把命中的攻略组装成参考上下文；超长按预算截断。

    owner_user_id 非空时，每条攻略会标注「我的攻略」或「公开攻略（作者：xxx）」，
    便于模型区分"用户自己的历史"与"别人公开分享的"，不会混为一谈。
    """
    parts, total = [], 0
    for i, g in enumerate(hits, 1):
        seg = f"【攻略{i}】{g.get('title', '未知标题')}（{g.get('destination', '未知')}"
        if g.get("days"):
            seg += f" {g['days']}天"
        seg += "）"
        if owner_user_id:
            if g.get("user_id") == owner_user_id:
                seg += "｜我的攻略"
            elif g.get("is_public"):
                author = g.get("nickname") or g.get("username") or "其他用户"
                seg += f"｜公开攻略（作者：{author}）"
        if g.get("rrf_score"):
            seg += f"  相关度:{g['rrf_score']:.4f}"
        if g.get("summary"):
            seg += f"\n摘要：{g['summary']}"
        if include_spots:
            content = g.get("content") or {}
            spots = [s.get("name") for s in (content.get("spots_catalog") or [])[:5] if s.get("name")]
            foods = [f.get("name") for f in (content.get("food_catalog") or [])[:3] if f.get("name")]
            if spots:
                seg += f"\n景点：{'、'.join(spots)}"
            if foods:
                seg += f"\n美食：{'、'.join(foods)}"
            prec = content.get("precautions_summary") or {}
            tips = [x for v in prec.values() if isinstance(v, list) for x in v][:3]
            if tips:
                seg += f"\n注意：{'；'.join(tips)}"

        if total + len(seg) > max_chars:
            break
        parts.append(seg)
        total += len(seg)

    if not parts:
        return ""
    intro = header or "以下是用户的历史旅游攻略，请结合它们回答（如与问题无关可忽略）："
    return intro + "\n\n" + "\n\n".join(parts)


# ============================================================
# 内置回归探针（验证三条通道各自有效）
# ============================================================
PROBES = [
    # (查询, 期望命中的 destination, 期望主要通道)
    ("上次去西安，兵马俑和大雁塔那两天是怎么安排的？", "西安", "es"),
    ("预算有限、人均 1500 左右的美食之旅去哪里好？", "成都", "facts"),
    ("想带小孩一起出去玩，节奏不要赶，有海有沙滩的度假攻略", "三亚", "semantics"),
    ("想住在古镇里面，晚上能看夜景坐摇橹船", "杭州+乌镇", "semantics"),
]


def verify(stores: Optional[GuideStores] = None, probes: Optional[list] = None,
           top_k: int = 5, quiet: bool = False) -> tuple:
    """跑内置探针：断言期望目的地在融合 top-k 内，并打印命中通道。返回 (all_passed, results)。"""
    own = stores is None
    stores = stores or GuideStores()
    probes = probes or PROBES
    results, passed = [], 0

    if not quiet:
        print("=" * 78)
        print("三库融合检索回归验证（ES 词法 + Qdrant A 事实 + Qdrant B 语义 → RRF → PG）")
        print("=" * 78)

    for query, expected, expect_channel in probes:
        res = search(query, stores=stores, top_k=top_k, debug_ranking=False)
        got = [h["destination"] for h in res["hits"]]
        ok = (expected in got) if expected else bool(res["hits"])
        passed += 1 if ok else 0
        results.append({"query": query, "expected": expected, "ok": ok,
                        "got": got, "channel_counts": res["channel_counts"],
                        "channels": [h["channels"] for h in res["hits"]]})

        if not quiet:
            print(f"\n▶ {query}")
            if expected:
                print(f"  期望：{expected}（主要通道 {expect_channel}）")
            cc = res["channel_counts"]
            print(f"  召回：{cc}")
            for i, h in enumerate(res["hits"], 1):
                flag = " ◀命中" if h["destination"] == expected else ""
                print(f"   #{i} [{h['destination']}] {h['title'][:30]} "
                      f"rrf={h['rrf_score']:.5f} via {', '.join(h['channels'])}{flag}")
            for w in res["warnings"]:
                print(f"   ⚠ {w}")
            print(f"  结果：{'PASS' if ok else 'FAIL'}")

    if own:
        stores.close()
    if not quiet:
        print("\n" + "-" * 78)
        print(f"回归验证：{passed}/{len(probes)} 通过")
    return passed == len(probes), results
