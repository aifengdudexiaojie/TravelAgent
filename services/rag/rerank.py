"""重排序（Rerank）服务
================================================================================
向量检索是"双塔"结构（query 与文档各自编码后算余弦），无法建模两者之间的
细粒度交互；重排序模型是"交叉编码器"（cross-encoder），把 (query, document)
拼在一起过一遍模型，直接判断"这段文档是否回答了这个问题"——正是本场景需要的
能力（例：query="有关诗歌的是哪里" → 文档含《长恨歌》→ 高度相关）。

后端可选（RERANK_PROVIDER）：
    dashscope  阿里云百炼 qwen3-rerank / qwen3.7-text-rerank（官方 API，推荐）
    llm        DeepSeek 列表式打分（零额外依赖，可返回打分理由）
    none       关闭重排序

环境变量
--------
    RERANK_PROVIDER   dashscope | llm | none（默认：配了 API Key 则 dashscope，否则 none）
    RERANK_MODEL      默认 qwen3-rerank（可选 qwen3.7-text-rerank，单文档上限 30k token）
    RERANK_URL        完整接口地址；缺省自动推导（见 _resolve_url）
    RERANK_INSTRUCT   排序指令（英文，可切换问答检索/语义相似策略）
    RERANK_TIMEOUT    超时秒数，默认 30
    DEEPSEEK_API_KEY  llm 后端所需的 Key

对外接口
--------
    rerank(query, documents, top_n=None, instruct=None) -> list[dict]
        返回 [{"index": i, "score": float(0~1), "reason": str|""}]，按 score 降序
    provider_info() -> dict    当前后端信息（用于日志/自检）
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from services.env_bootstrap import load_env

load_env()

# 默认指令：问答检索（侧重"是否回答了问题"，而非泛泛主题相似）
DEFAULT_INSTRUCT = "Given a web search query, retrieve relevant passages that answer the query."


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _resolve_url() -> str:
    """推导 rerank 接口地址。

    优先 RERANK_URL；否则由 EMBEDDING_BASE_URL 推导（同域名的 DashScope 原生 rerank 路径）；
    再否则用公共 DashScope 地址。
    """
    explicit = _env("RERANK_URL")
    if explicit:
        return explicit

    emb_base = _env("EMBEDDING_BASE_URL")
    if emb_base:
        # 例：https://xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
        #  →  https://xxx.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
        m = re.match(r"^(https?://[^/]+)", emb_base)
        if m:
            host = m.group(1)
            if "maas.aliyuncs.com" in host or "dashscope" in host:
                return f"{host}/api/v1/services/rerank/text-rerank/text-rerank"
    return "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"


def _api_key() -> str:
    return _env("RERANK_API_KEY") or _env("DASHSCOPE_API_KEY") or _env("EMBEDDING_API_KEY")


def _provider() -> str:
    p = _env("RERANK_PROVIDER").lower()
    if p in ("dashscope", "llm", "none"):
        return p
    # 未显式配置：有百炼 Key 就默认用官方 rerank；否则关闭（避免静默失败）
    return "dashscope" if _api_key() else "none"


def provider_info() -> dict:
    p = _provider()
    return {
        "provider": p,
        "model": _env("RERANK_MODEL", "qwen3-rerank") if p == "dashscope"
                 else (_env("DEEPSEEK_MODEL", "deepseek-chat") if p == "llm" else ""),
        "url": _resolve_url() if p == "dashscope" else "",
        "instruct": _env("RERANK_INSTRUCT", DEFAULT_INSTRUCT),
        "enabled": p != "none",
    }


# ============================================================
# 后端 1：阿里云百炼 rerank（交叉编码器）
# ============================================================
def _rerank_dashscope(query: str, documents: List[str],
                      top_n: Optional[int], instruct: Optional[str]) -> List[dict]:
    import httpx

    url = _resolve_url()
    key = _api_key()
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY / RERANK_API_KEY")

    payload = {
        "model": _env("RERANK_MODEL", "qwen3-rerank"),
        "query": query,
        "documents": documents,
    }
    if top_n:
        payload["top_n"] = min(top_n, len(documents))
    ins = instruct or _env("RERANK_INSTRUCT", DEFAULT_INSTRUCT)
    if ins:
        payload["instruct"] = ins

    timeout = float(_env("RERANK_TIMEOUT", "30") or 30)
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"rerank HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    # DashScope 原生返回 {"output": {"results": [{"index":..,"relevance_score":..}]}}
    # OpenAI 兼容返回 {"results": [...]}；两种都兼容
    results = (data.get("output") or {}).get("results") or data.get("results") or []
    if not results:
        raise RuntimeError(f"rerank 返回缺少 results 字段：{str(data)[:200]}")

    out = []
    for item in results:
        idx = item.get("index")
        if idx is None:
            continue
        score = item.get("relevance_score", item.get("score", 0.0))
        out.append({"index": int(idx), "score": float(score), "reason": ""})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ============================================================
# 后端 2：LLM 列表式打分（DeepSeek，可给理由）
# ============================================================
_LLM_SYSTEM = """你是检索结果相关性评审员。
给定一个用户查询和若干候选文档，请判断每个候选与查询的相关程度，输出 0-10 分：
  10 = 直接回答了查询（含查询所指的具体事实/名称/典故）
   7 = 高度相关，能作为回答依据
   4 = 主题沾边但没回答问题
   1 = 几乎无关
注意：不要因为文档篇幅长、景点多就给高分；要判断"是否命中查询真正在问的东西"。
必须输出严格 JSON 数组（不要 Markdown 代码块、不要解释），每项形如：
[{"index": 0, "score": 7, "reason": "一句话理由"}]"""


def _rerank_llm(query: str, documents: List[str],
                top_n: Optional[int], instruct: Optional[str]) -> List[dict]:
    from openai import OpenAI

    key = _env("DEEPSEEK_API_KEY") or _env("KIMI_API_KEY")
    if not key:
        raise RuntimeError("llm rerank 需要 DEEPSEEK_API_KEY / KIMI_API_KEY")
    base_url = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = _env("DEEPSEEK_MODEL", "deepseek-chat")

    listing = "\n\n".join(
        f"[{i}] {doc[:600]}" for i, doc in enumerate(documents)
    )
    client = OpenAI(api_key=key, base_url=base_url,
                    timeout=float(_env("RERANK_TIMEOUT", "60") or 60))
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": f"查询：{query}\n\n候选文档：\n{listing}"},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\[.*\]", raw, re.S)
    data = json.loads(m.group(0) if m else raw)

    out = []
    for item in data:
        if not isinstance(item, dict) or "index" not in item:
            continue
        try:
            idx = int(item["index"])
            score = float(item.get("score", 0)) / 10.0      # 归一到 0~1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(documents):
            out.append({"index": idx, "score": score,
                        "reason": str(item.get("reason", ""))[:120]})
    if not out:
        raise RuntimeError("llm rerank 未返回有效结果")
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_n] if top_n else out


# ============================================================
# 统一入口
# ============================================================
def rerank(query: str, documents: List[str], top_n: Optional[int] = None,
           instruct: Optional[str] = None) -> List[dict]:
    """对候选文档重排序。失败时抛异常（由调用方决定降级策略）。"""
    if not documents:
        return []
    provider = _provider()
    if provider == "none":
        raise RuntimeError("RERANK_PROVIDER=none（未启用重排序）")
    if provider == "llm":
        return _rerank_llm(query, documents, top_n, instruct)
    return _rerank_dashscope(query, documents, top_n, instruct)
