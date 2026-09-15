"""RAG 查询编排与结果展示（函数化，供 CLI 与其它程序调用）
================================================================================
链路：ES 词法 + 每个配置的 Qdrant 向量集合各召回一路 → RRF 融合去重
      → 交叉编码重排序 → 按 guide_id 回 PostgreSQL 取全文 → 组装 RAG 上下文

原 rag_query.py 的全部命令行参数都保留为 QueryOptions 字段，CLI 只是它的薄封装：

    from services.rag.query import QueryOptions, execute_query, format_result

    options = QueryOptions(top_k=5, show_context=True)
    result = execute_query("上次去西安兵马俑怎么安排", options)
    print(format_result(result, options))
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from services.env_bootstrap import load_env

load_env()

from services.rag.retrieval import (        # noqa: E402
    PROBES,
    build_context,
    check_embedding_consistency,
    search,
    verify,
)
from services.rag.store import GuideStores, preflight_services  # noqa: E402


# ============================================================
# 参数（对应原 CLI 的全部选项）
# ============================================================
@dataclass
class QueryOptions:
    top_k: int = 5                      # 原 --top-k
    per_source: int = 10                # 原 --per-source
    user_id: Optional[str] = None       # 原 --user-id
    public_only: bool = False           # 原 --public-only
    strict_embedding: bool = False      # 原 --strict-embedding
    use_rerank: Optional[bool] = None   # 原 --no-rerank（False=关闭；None=按 env）
    debug_ranking: Optional[bool] = None  # 原 --no-debug-ranking（False=不打印排序明细）
    show_context: bool = False          # 原 --context
    context_chars: int = 3000           # 原 --context-chars
    snippet: int = 90                   # 摘要截断长度
    json_output: bool = False           # 原 --json
    show_embedding: bool = True         # 是否打印向量化配置
    skip_service_check: bool = False    # 原 --skip-service-check


# ============================================================
# 展示
# ============================================================
def format_embedding_info(stores: GuideStores) -> str:
    """向量化配置与一致性（明确"查询用的是哪个模型"）。"""
    info = stores.embedding_info
    c = check_embedding_consistency(stores)
    lines = [
        "── 向量化配置 ──────────────────────────────────────────",
        f"  查询用 embedding : {info['model']}  (mode={info['mode']}, dim={info['dim']})",
        f"  接口地址         : {info['base_url'] or '（本地模型）'}",
    ]
    if c["consistent"] is True:
        lines.append(f"  入库一致性       : ✅ 与入库时一致（{c['stored']}）")
    elif c["consistent"] is False:
        lines.append(f"  入库一致性       : ❌ 不一致！入库=[{c['stored']}] vs 查询=[{c['current']}]")
    else:
        lines.append(f"  入库一致性       : ⚠ 未登记（当前={c['current']}）")
    lines.append("────────────────────────────────────────────────────────")
    return "\n".join(lines)


def format_result(result: dict, options: Optional[QueryOptions] = None) -> str:
    """把一次查询结果格式化成可打印文本。"""
    options = options or QueryOptions()
    cc = result["channel_counts"]
    emb = result.get("embedding") or {}
    emb_tag = {True: "一致", False: "❌不一致", None: "未登记"}.get(emb.get("consistent"), "?")

    lines = [f"\n查询：{result['query']}"]
    lines.append("各通道召回：" + "  ".join(f"{k}={v}" for k, v in cc.items())
                 + f"   向量可用={'是' if result['vector_ok'] else '否（仅词法）'}"
                 + f"   向量空间={emb_tag}")
    for w in result["warnings"]:
        lines.append(f"  ⚠ {w}")

    if not result["hits"]:
        lines.append("  （无命中）")
        return "\n".join(lines)

    lines.append(f"\n融合结果（top {len(result['hits'])}）：")
    rk = result.get("rerank") or {}
    if rk.get("enabled") and rk.get("provider"):
        lines.append(f"  重排序后端：{rk['provider']} / {rk.get('model', '')}"
                     + (f"  ⚠ {rk['error']}" if rk.get("error") else ""))
    for i, h in enumerate(result["hits"], 1):
        lines.append(f"  #{i} [{h['destination']}] {h['title']}")
        score_bits = []
        if h.get("final_score") is not None:
            score_bits.append(f"final={h['final_score']:.4f}")
        if h.get("rerank_score") is not None:
            score_bits.append(f"rerank={h['rerank_score']:.4f}")
        score_bits.append(f"rrf={h['rrf_score']:.5f}")
        lines.append(f"      {'  '.join(score_bits)}  via {', '.join(h['channels'])}")
        lines.append(f"      guide_id={h['guide_id']}")
        if h.get("days"):
            lines.append(f"      天数={h['days']}  关键词={len(h.get('keywords') or [])} 个")
        if h.get("rerank_reason"):
            lines.append(f"      重排序理由：{h['rerank_reason']}")
        if h.get("summary"):
            text = h["summary"]
            lines.append(f"      摘要：{text[:options.snippet]}"
                         + ("…" if len(text) > options.snippet else ""))

    if options.show_context:
        ctx = build_context(result["hits"], max_chars=options.context_chars)
        lines += ["\n" + "=" * 78,
                  f"RAG 上下文（{len(ctx)} 字，将作为参考注入 LLM）：",
                  "=" * 78,
                  ctx if ctx else "（空：没有可用于组装的攻略）"]
    return "\n".join(lines)


def print_result(result: dict, options: Optional[QueryOptions] = None) -> None:
    options = options or QueryOptions()
    if options.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_result(result, options))


# ============================================================
# 查询执行
# ============================================================
def run_query(query_text: str, options: Optional[QueryOptions] = None,
              stores: Optional[GuideStores] = None) -> dict:
    """执行一次检索（不打印、不建连接；stores 为 None 时内部建立并关闭）。"""
    options = options or QueryOptions()
    own = stores is None
    stores = stores or GuideStores()
    try:
        return search(query_text, stores=stores, top_k=options.top_k,
                      per_source=options.per_source,
                      user_id=options.user_id, public_only=options.public_only,
                      strict_embedding=options.strict_embedding,
                      use_rerank=options.use_rerank,
                      debug_ranking=options.debug_ranking)
    finally:
        if own:
            stores.close()


def execute_query(query_text: str, options: Optional[QueryOptions] = None,
                  print_output: bool = True) -> dict:
    """带依赖预检的完整查询流程（原 main 的查询分支）。

    返回 search() 的结果字典；print_output=True 时顺带打印配置与命中。
    依赖服务未就绪时抛 RuntimeError。
    """
    options = options or QueryOptions()

    if not options.skip_service_check:
        all_ok, results = preflight_services(quiet=options.json_output)
        if not all_ok:
            missing = "、".join(r["service"] for r in results if not r["ok"])
            raise RuntimeError(f"依赖服务未就绪：{missing}（如确认已就绪可 skip_service_check=True）")

    stores = GuideStores()
    try:
        if print_output and options.show_embedding and not options.json_output:
            print(format_embedding_info(stores))
        result = run_query(query_text, options, stores=stores)
    finally:
        stores.close()

    if print_output:
        print_result(result, options)
    return result


def verify_all(options: Optional[QueryOptions] = None,
               stores: Optional[GuideStores] = None) -> Tuple[bool, list]:
    """跑内置回归探针（原 --verify）。"""
    options = options or QueryOptions()
    own = stores is None
    stores = stores or GuideStores()
    try:
        all_ok, results = verify(stores=stores)
    finally:
        if own:
            stores.close()
    return all_ok, results


# ============================================================
# 交互模式
# ============================================================
def repl(stores: GuideStores, options: Optional[QueryOptions] = None) -> int:
    """交互式查询循环（原 repl；输入 exit/quit 退出，/verify 跑回归）。"""
    options = options or QueryOptions()
    print("\n进入交互式查询（输入 exit / quit 退出；/verify 跑回归验证）")
    while True:
        try:
            query_text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query_text:
            continue
        if query_text.lower() in ("exit", "quit", "q"):
            break
        if query_text == "/verify":
            verify(stores=stores)
            continue
        if query_text in ("/help", "help"):
            print("  直接输入问题即可查询；/verify 跑回归验证；exit 退出")
            continue
        result = run_query(query_text, options, stores=stores)
        print_result(result, options)
    return 0


__all__ = [
    "PROBES", "QueryOptions", "execute_query", "format_embedding_info",
    "format_result", "print_result", "repl", "run_query", "verify_all",
]
