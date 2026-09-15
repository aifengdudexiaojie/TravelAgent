"""入库 CLI（薄封装）—— 逻辑全在 services/rag/ingest.py
================================================================================
唯一转换入口：services/rag/transform.es_info_transfer（攻略 JSON → es_doc / vector_content）
写入目标：
    PostgreSQL     guides 表（content_json 真相源 + 身份列，不含派生文本）
    Elasticsearch  guide_search_docs（es_doc，cjk 中文分词，自动补齐字段映射）
    Qdrant         guide_chunks（vector_content 分块向量；集合名可配）

用法（参数与旧版完全一致）
    python ingest_by_es_transfer.py                                  # 4 篇内置示例
    python ingest_by_es_transfer.py --input example.json --destination 香港 --title "香港3天攻略"
    python ingest_by_es_transfer.py --dry-run --input example.json   # 只转换预览，不连库
    python ingest_by_es_transfer.py --no-es --no-qdrant              # 只写 PG 真相源
    python ingest_by_es_transfer.py --reset-derived                  # 重建 ES 索引与目标集合
    python ingest_by_es_transfer.py --stats                          # 只看三库数据量
    python ingest_by_es_transfer.py --collection my_chunks           # 指定 Qdrant 集合
"""

from __future__ import annotations

import argparse
import sys

from services.env_bootstrap import load_env

load_env()

from services.rag.ingest import (      # noqa: E402
    CHUNK_COLLECTION,
    IngestOptions,
    ingest_guides,
    load_guides,
    parse_trust,
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="按 es_info_transfer 输出入库：PG 真相源 + ES 检索文档 + Qdrant 向量块")
    ap.add_argument("--input", metavar="PATH", help="攻略 JSON 文件；缺省用内置示例数据")
    ap.add_argument("--title")
    ap.add_argument("--destination")
    ap.add_argument("--days", type=int)
    ap.add_argument("--summary")
    ap.add_argument("--user-id")
    ap.add_argument("--username")
    ap.add_argument("--nickname")
    ap.add_argument("--trust", help="信任度/来源等元数据；JSON、数字或纯文本")
    ap.add_argument("--collection", default=CHUNK_COLLECTION,
                    help=f"Qdrant 集合名（默认 {CHUNK_COLLECTION}）")
    ap.add_argument("--no-es", action="store_true", help="跳过 Elasticsearch")
    ap.add_argument("--no-qdrant", action="store_true", help="跳过 Qdrant")
    ap.add_argument("--reset-derived", action="store_true",
                    help="重建 ES 索引与目标 Qdrant 集合（改映射/换 embedding 模型后使用）")
    ap.add_argument("--dry-run", action="store_true", help="只做转换与预览，不连库")
    ap.add_argument("--stats", action="store_true", help="只打印三库统计")
    ap.add_argument("--skip-service-check", action="store_true", help="跳过依赖服务预检")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    options = IngestOptions(
        title=args.title,
        destination=args.destination,
        days=args.days,
        summary=args.summary,
        user_id=args.user_id,
        username=args.username,
        nickname=args.nickname,
        trust=parse_trust(args.trust) if args.trust is not None else None,
        collection=args.collection,
        no_es=args.no_es,
        no_qdrant=args.no_qdrant,
        reset_derived=args.reset_derived,
        dry_run=args.dry_run,
        stats_only=args.stats,
        skip_service_check=args.skip_service_check,
        verbose=True,
    )

    result = ingest_guides(load_guides(args.input), options=options)
    if result.get("dry_run"):
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except Exception as exc:
        import traceback
        print(f"\n❌ 入库失败：{exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
