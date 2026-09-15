"""RAG 查询 CLI（薄封装）—— 逻辑全在 services/rag/query.py 与 services/rag/retrieval.py
================================================================================
链路：ES 词法 + 每个配置的 Qdrant 集合各召回一路 → RRF 融合去重
      → 交叉编码重排序 → 回 PostgreSQL 取全文 → 组装 RAG 上下文

用法（参数与旧版完全一致）
    python rag_query.py "上次去西安，兵马俑那两天怎么安排的？"
    python rag_query.py                       # 交互式（exit 退出，/verify 跑回归）
    python rag_query.py --verify              # 回归探针
    python rag_query.py -q "带小孩的海边攻略" --context
    python rag_query.py -q "成都美食" --top-k 5 --per-source 10
    python rag_query.py -q "我的攻略" --user-id rag_test_user --public-only
    python rag_query.py -q "西安历史" --json
    python rag_query.py -q "..." --no-rerank --no-debug-ranking
"""

from __future__ import annotations

import argparse
import sys

from services.env_bootstrap import load_env

load_env()

from services.rag.query import (        # noqa: E402
    QueryOptions,
    execute_query,
    format_embedding_info,
    repl,
    verify_all,
)
from services.rag.store import GuideStores, preflight_services  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="RAG 查询（PG + ES + Qdrant 融合检索）")
    ap.add_argument("query", nargs="?", help="查询语句；不传则进入交互模式")
    ap.add_argument("-q", "--query", dest="query_opt", help="查询语句（等价于位置参数）")
    ap.add_argument("--top-k", type=int, default=5, help="融合后返回条数（默认 5）")
    ap.add_argument("--per-source", type=int, default=10, help="每路召回条数（默认 10）")
    ap.add_argument("--context", action="store_true", help="打印 RAG 上下文")
    ap.add_argument("--context-chars", type=int, default=3000, help="RAG 上下文字数上限")
    ap.add_argument("--user-id", help="只看某个用户的攻略（ES 过滤 + PG 校验）")
    ap.add_argument("--public-only", action="store_true", help="只看已公开的攻略")
    ap.add_argument("--verify", action="store_true", help="跑内置回归探针并退出")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    ap.add_argument("--strict-embedding", action="store_true",
                    help="查询用 embedding 与入库时不一致则直接报错（默认仅告警）")
    ap.add_argument("--no-rerank", action="store_true",
                    help="关闭交叉编码重排序（只按 RRF 排序，便于对比排查）")
    ap.add_argument("--no-debug-ranking", action="store_true",
                    help="不打印各通道召回排序明细（默认打印；也可用 RAG_DEBUG_RANKING=false）")
    ap.add_argument("--skip-service-check", action="store_true", help="跳过依赖服务预检")
    return ap


def options_from_args(args: argparse.Namespace) -> QueryOptions:
    return QueryOptions(
        top_k=args.top_k,
        per_source=args.per_source,
        user_id=args.user_id,
        public_only=args.public_only,
        strict_embedding=args.strict_embedding,
        use_rerank=False if args.no_rerank else None,
        debug_ranking=False if args.no_debug_ranking else None,
        show_context=args.context,
        context_chars=args.context_chars,
        json_output=args.json,
        show_embedding=not args.json,
        skip_service_check=args.skip_service_check,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    query_text = args.query_opt or args.query
    options = options_from_args(args)

    # 依赖预检（交互模式与 --verify 自行处理；单次查询由 execute_query 内部处理）
    if not options.skip_service_check and (args.verify or not query_text):
        all_ok, results = preflight_services(quiet=options.json_output)
        if not all_ok:
            missing = "、".join(r["service"] for r in results if not r["ok"])
            print(f"\n❌ 依赖服务未就绪：{missing}（如确认已就绪可加 --skip-service-check）")
            return 1

    # --verify：回归探针
    if args.verify:
        stores = GuideStores()
        try:
            if options.show_embedding:
                print(format_embedding_info(stores))
            all_passed, _ = verify_all(options, stores=stores)
        finally:
            stores.close()
        return 0 if all_passed else 1

    # 交互模式
    if not query_text:
        stores = GuideStores()
        try:
            if options.show_embedding and not options.json_output:
                print(format_embedding_info(stores))
            return repl(stores, options)
        finally:
            stores.close()

    # 单次查询
    result = execute_query(query_text, options)
    return 0 if result["hits"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except Exception as exc:
        import traceback
        print(f"\n❌ 执行出错：{exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
