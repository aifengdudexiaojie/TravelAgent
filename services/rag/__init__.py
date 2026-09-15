"""RAG 子包：入库、检索、重排序与查询编排
================================================================================
目录结构
    transform.py  唯一文本转换入口 es_info_transfer()（攻略 JSON → es_doc / vector_content）
    store.py      三库基础设施（PG 真相源 / ES 词法 / Qdrant 向量）+ 依赖预检
    ingest.py     入库编排（函数化，参数见 IngestOptions）
    retrieval.py  检索（多路召回 → RRF → 重排 → PG 回填，参数见 search()）
    rerank.py     交叉编码重排序（dashscope / llm / none）
    query.py      查询编排与展示（函数化，参数见 QueryOptions）

典型用法
    # 入库
    from services.rag import IngestOptions, ingest_guides
    ingest_guides(options=IngestOptions())

    # 检索
    from services.rag import QueryOptions, execute_query, format_result
    result = execute_query("上次去西安兵马俑怎么安排", QueryOptions(top_k=5))
"""

from services.rag.transform import es_info_transfer
from services.rag.store import (
    GUIDE_INDEX,
    VECTOR_COLLECTIONS,
    GuideStores,
    check_es,
    check_pg,
    check_qdrant,
    config_report,
    es_apply_mapping,
    es_client,
    es_ensure_index,
    es_index_doc,
    build_search_doc,
    guide_id_for,
    pg_fetch_guides,
    pg_session,
    preflight_embedding,
    preflight_services,
    qdrant_client,
    qdrant_ensure_collections,
    qdrant_upsert,
    storage_stats,
)
from services.rag.ingest import (
    CHUNK_COLLECTION,
    IngestOptions,
    build_preview,
    delete_guide,
    ingest_from_file,
    ingest_guides,
    load_guides,
    normalize_record,
    stats,
    typed_chunks,
)
from services.rag.rerank import provider_info as rerank_provider_info
from services.rag.rerank import rerank
from services.rag.validate import (
    RagValidationError,
    SummaryFormatError,
    SummaryStructureError,
    clean_json_text,
    parse_summary_json,
    prepare_summary_for_ingest,
    summary_to_record,
    validate_and_prepare,
    validate_summary,
)
from services.rag.retrieval import (
    PROBES,
    build_context,
    build_candidate_text,
    check_embedding_consistency,
    es_lexical_search,
    finalize_ranking,
    qdrant_search,
    rrf_fuse,
    search,
    verify,
)
from services.rag.query import (
    QueryOptions,
    execute_query,
    format_embedding_info,
    format_result,
    print_result,
    repl,
    run_query,
    verify_all,
)

__all__ = [
    # transform
    "es_info_transfer",
    # store
    "GUIDE_INDEX", "VECTOR_COLLECTIONS", "GuideStores",
    "check_es", "check_pg", "check_qdrant", "config_report",
    "es_apply_mapping", "es_client", "es_ensure_index", "es_index_doc",
    "build_search_doc", "guide_id_for", "pg_fetch_guides", "pg_session",
    "preflight_embedding", "preflight_services",
    "qdrant_client", "qdrant_ensure_collections", "qdrant_upsert", "storage_stats",
    # ingest
    "CHUNK_COLLECTION", "IngestOptions", "build_preview", "delete_guide",
    "ingest_from_file", "ingest_guides", "load_guides", "normalize_record",
    "stats", "typed_chunks",
    # rerank
    "rerank", "rerank_provider_info",
    # validate
    "validate_summary", "validate_and_prepare", "summary_to_record",
    "parse_summary_json", "clean_json_text", "prepare_summary_for_ingest",
    "RagValidationError", "SummaryFormatError", "SummaryStructureError",
    # retrieval
    "PROBES", "build_context", "build_candidate_text", "check_embedding_consistency",
    "es_lexical_search", "finalize_ranking", "qdrant_search", "rrf_fuse", "search", "verify",
    # query
    "QueryOptions", "execute_query", "format_embedding_info", "format_result",
    "print_result", "repl", "run_query", "verify_all",
]
