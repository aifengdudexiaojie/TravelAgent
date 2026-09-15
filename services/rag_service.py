"""RAG 服务 —— 历史攻略的「意图判定 + 检索 + 上下文构建」
================================================================================
对外三件事（供聊天链路使用）：

  1. 意图判定   agent_rag_intent(query) / should_use_rag(query)
        · 先走关键词快路径（"上次/以前/去年/…"）
        · 未命中则交给 LLM + skills/rag-intent.md 做语义判定，
          并返回**改写后的检索语句**与关键词
  2. 历史检索   search_related_guides(query, top_k)
        · 走统一 RAG 栈：ES 词法 + Qdrant 向量 → RRF → 交叉编码重排 → PG 回填全文
  3. 上下文构建 build_rag_context(guides, max_chars)
        · 组装成可注入 system prompt 的参考文本

上层（services/chat_service.py）根据"历史模式"开关决定是否调用本模块。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.tips_agent import GeneralAgent
from services.rag.retrieval import build_context as _build_context_from_hits
from services.rag.retrieval import search as _rag_search
from utils.toJson import to_json

logger = logging.getLogger(__name__)

# 意图判定用的模型与 skill（可用环境变量覆盖）
RAG_INTENT_MODEL = os.getenv("RAG_INTENT_MODEL", "deepseek-free")
RAG_INTENT_SKILL = os.getenv("RAG_INTENT_SKILL", "rag-intent.md")

# 聊天"历史模式"的默认可见性：own = 只检索当前用户自己的攻略。
# 为什么默认 own：历史模式问的是"我自己以前去过哪、怎么安排的"，
# 检索到别人的（或测试写入的）攻略会答非所问、还会泄露他人数据。
# 可选 own_or_public（自己的 + 别人公开的）/ public / all，用环境变量覆盖。
CHAT_VISIBILITY = os.getenv("RAG_CHAT_VISIBILITY", "own").strip().lower() or "own"

# 聊天模式 → 检索范围（谁能被检索到）
#   normal   不检索
#   history  只看自己的（历史模式）
#   public   自己的 + 他人已公开的（公开模式）
MODE_VISIBILITY: Dict[str, str] = {
    "normal": "all",            # 不检索，占位
    "history": "own",
    "public": "own_or_public",
}

# 各模式注入上下文时的说明文字（避免模型把别人的公开攻略说成"你以前去过"）
MODE_CONTEXT_HEADER: Dict[str, str] = {
    "history": "以下是这位用户**自己以前保存的**旅游攻略（属于他的个人历史），"
               "请结合它们回答；如果与当前问题无关可以忽略。",
    "public": "以下是可参考的旅游攻略库内容：标注为「我的攻略」的是这位用户自己的记录，"
              "标注为「公开攻略」的是其他用户公开分享的。回答时可以引用这些信息，"
              "但**不要把别人的公开攻略当成这位用户自己的经历**（不要用「你上次去过」这类说法）。",
}


def chat_visibility() -> str:
    """聊天链路默认的可见性模式（历史模式用；非法值回退 own）。"""
    from services.rag.retrieval import VISIBILITIES
    return CHAT_VISIBILITY if CHAT_VISIBILITY in VISIBILITIES else "own"


def mode_visibility(mode: str) -> str:
    """聊天模式 → 可见性。history 默认只看自己的；public 看自己 + 公开的。

    环境变量 RAG_CHAT_VISIBILITY 只用于覆盖 history 模式的默认值
    （public 模式固定 own_or_public，否则"公开模式"就没意义了）。
    """
    if mode == "history":
        return chat_visibility()
    return MODE_VISIBILITY.get(mode, "own")


# 明确的"回忆既往经历"关键词：命中即快速判定为需要检索，无需调用 LLM
HISTORY_KEYWORDS: Tuple[str, ...] = (
    # 过去
    "以前", "过去", "之前", "以往", "曾经", "从前", "过往", "早些时候", "之前的时候",
    # 上一次
    "上次", "上回", "上一回", "上一次", "前一次", "前回", "之前那次",
    # 最近
    "最近一次", "最近去过", "最近住过", "最近那次",
    # 时间周期
    "去年", "前年", "上个月", "上周", "前几天", "去年国庆", "去年春节", "去年暑假",
    # 个人经历指代
    "我去过", "我住的", "我吃过", "我的攻略", "我的行程", "我那次", "我收藏",
)

# 旅游领域常见词（用于规则兜底判断与关键词提取）
TRAVEL_HINTS: Tuple[str, ...] = (
    "旅游", "旅行", "攻略", "行程", "景点", "美食", "住宿", "酒店", "民宿", "客栈",
    "机票", "高铁", "自驾", "预算", "门票", "路线", "玩", "逛", "出发", "度假",
)

# 简单的目的地提取（规则兜底；语义判定交给 LLM）
_PLACE_RE = re.compile(r"([\u4e00-\u9fa5]{2,6})(?:市|省|县|区|古镇|景区)?")

_STOPWORDS = {"那个", "这个", "什么", "怎么", "哪里", "哪儿", "来着", "你还记得", "帮我",
              "我想", "可以", "还有", "是不是", "请问", "一下", "记得"}

# 小型判定缓存（同一问题短期内不重复调用 LLM）
_DECISION_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LIMIT = 256


# ============================================================
# 关键词层
# ============================================================
def extract_keywords_from_query(query: str) -> List[str]:
    """从查询中提取可能的目的地/关键词（规则版；供兜底与关键词快路径使用）。"""
    if not query:
        return []

    keywords: List[str] = []

    travel_patterns = [
        r"去(.{2,6}?)(?:旅游|玩|旅行|逛|的)",
        r"(.{2,6}?)(?:旅游|攻略|景点|美食|住宿|行程)",
        r"(.{2,4}?)(?:几日游|一日游|两日游|三日游|\d+天)",
    ]
    for pattern in travel_patterns:
        for match in re.findall(pattern, query):
            loc = match if isinstance(match, str) else match[0]
            loc = loc.strip()
            if 2 <= len(loc) <= 8 and loc not in _STOPWORDS:
                keywords.append(loc)

    for kw in HISTORY_KEYWORDS:
        if kw in query:
            keywords.append(kw)
    for kw in TRAVEL_HINTS:
        if kw in query:
            keywords.append(kw)

    # 去重保序，最多 6 个
    return list(dict.fromkeys(k for k in keywords if k))[:6]


def keyword_prefilter(query: str) -> Tuple[Optional[bool], List[str], str]:
    """关键词快路径。

    返回 (need_rag, matched_keywords, reason)：
        need_rag = True  → 命中明确的历史回忆词，可直接判定需要检索
        need_rag = None  → 规则无法判定，交给 LLM 语义判定
    """
    q = (query or "").strip()
    if not q:
        return False, [], "空问题，无需检索"

    matched = [kw for kw in HISTORY_KEYWORDS if kw in q]
    if matched:
        return True, matched, f"命中历史回忆关键词：{'、'.join(matched[:4])}"
    return None, [], "关键词快路径未命中，交由 LLM 语义判定"


# ============================================================
# LLM 意图判定（skills/rag-intent.md）
# ============================================================
def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "是", "需要", "true。")
    return False


def _normalize_decision(data: Any, query: str, fallback_reason: str = "") -> Dict[str, Any]:
    """把 LLM 返回（或兜底结果）规整成统一结构。"""
    if not isinstance(data, dict):
        return _heuristic_decision(query, fallback_reason or "LLM 返回非对象")

    need_rag = _coerce_bool(data.get("need_rag", data.get("needRag")))
    keywords = data.get("keywords") or data.get("keyword") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r"[,，、\s]+", keywords) if k.strip()]
    keywords = [str(k) for k in keywords if str(k).strip()][:6]

    rag_query = str(data.get("query") or data.get("rewritten_query") or "").strip()
    if need_rag and not rag_query:
        rag_query = query            # 未改写则用原问题
    if not need_rag:
        rag_query = ""

    return {
        "need_rag": need_rag,
        "intent_type": str(data.get("intent_type") or data.get("type") or "unknown"),
        "reason": str(data.get("reason") or fallback_reason or ""),
        "query": rag_query,
        "keywords": keywords or extract_keywords_from_query(query),
        "source": "llm",
    }


def _heuristic_decision(query: str, reason: str = "") -> Dict[str, Any]:
    """LLM 不可用时的规则兜底判定。"""
    need, matched, why = keyword_prefilter(query)
    if need is None:
        # 无历史词：仅当既提到旅游领域又含第一人称经历时才判需要检索
        personal = any(p in query for p in ("我", "我的", "自己"))
        travel = any(t in query for t in TRAVEL_HINTS)
        need = bool(personal and travel)
        why = ("含第一人称 + 旅游领域词，规则兜底判为需要检索"
               if need else "未发现个人历史线索，规则兜底判为无需检索")
    return {
        "need_rag": bool(need),
        "intent_type": "history_recall" if need else "general_travel",
        "reason": (reason + "｜" if reason else "") + why,
        "query": query if need else "",
        "keywords": matched or extract_keywords_from_query(query),
        "source": "heuristic",
    }


async def agent_rag_intent(query: str) -> Dict[str, Any]:
    """用 LLM + skills/rag-intent.md 判定是否需要历史检索，并给出改写后的检索语句。

    返回：{"need_rag": bool, "intent_type": str, "reason": str,
           "query": str, "keywords": [str], "source": "llm|heuristic|keyword"}
    """
    q = (query or "").strip()
    if not q:
        return _heuristic_decision(q, "空问题")

    cached = _DECISION_CACHE.get(q)
    if cached:
        return dict(cached)

    # 关键词快路径：命中直接返回，省一次 LLM 调用
    need, matched, reason = keyword_prefilter(q)
    if need is True:
        decision = {"need_rag": True, "intent_type": "history_recall", "reason": reason,
                    "query": q, "keywords": matched, "source": "keyword"}
    else:
        try:
            agent = GeneralAgent(RAG_INTENT_MODEL, RAG_INTENT_SKILL)
            raw = await agent.chat([{"role": "user", "content": q}])
            decision = _normalize_decision(to_json(raw), q)
        except Exception as exc:                     # LLM/解析失败 → 规则兜底
            logger.warning(f"RAG 意图判定失败，退化为规则兜底: {exc}")
            decision = _heuristic_decision(q, f"LLM 判定失败（{type(exc).__name__}）")

    if len(_DECISION_CACHE) >= _CACHE_LIMIT:
        _DECISION_CACHE.clear()
    _DECISION_CACHE[q] = decision
    logger.info(f"RAG 意图判定：need_rag={decision['need_rag']} "
                f"type={decision['intent_type']} source={decision['source']} reason={decision['reason']}")
    return dict(decision)


async def should_use_rag(query: str, *, force_agent: bool = False) -> Tuple[bool, str]:
    """是否需要检索历史攻略。返回 (need_rag, reason)。

    force_agent=True 时跳过关键词快路径，强制走 LLM 判定（便于调试/对比）。
    """
    if not force_agent:
        need, _, reason = keyword_prefilter(query)
        if need is not None:
            return need, reason
    decision = await agent_rag_intent(query)
    return bool(decision["need_rag"]), str(decision["reason"])


# ============================================================
# 历史攻略检索（统一 RAG 栈）
# ============================================================
def search_related_guides(query: str, top_k: int = 5, *,
                          user_id: Optional[str] = None,
                          public_only: bool = False,
                          visibility: Optional[str] = None,
                          per_source: int = 10,
                          debug_ranking: bool = False) -> List[dict]:
    """检索历史攻略：ES 词法 + Qdrant 向量 → RRF → 交叉编码重排 → PG 回填全文。

    visibility 控制"能检索到谁的攻略"（见 retrieval.resolve_visibility）：
    聊天场景传 chat_visibility()（默认 own，只检索本人攻略）。

    返回命中列表（按最终得分降序），每条附加 `_score`（final/rerank/rrf 依次回退），
    保留旧字段名以便旧代码（build_rag_context）无需改动。
    """
    hits, _ = search_related_guides_with_meta(
        query, top_k=top_k, user_id=user_id, public_only=public_only,
        visibility=visibility, per_source=per_source, debug_ranking=debug_ranking)
    return hits


def search_related_guides_with_meta(query: str, top_k: int = 5, *,
                                    user_id: Optional[str] = None,
                                    public_only: bool = False,
                                    visibility: Optional[str] = None,
                                    per_source: int = 10,
                                    debug_ranking: bool = False,
                                    ) -> Tuple[List[dict], Dict[str, Any]]:
    """同上，但额外返回原始检索结果（含各通道召回统计、重排序信息）。"""
    mode = visibility or chat_visibility()
    try:
        result = _rag_search(query, top_k=top_k, per_source=per_source,
                             user_id=user_id, public_only=public_only,
                             visibility=mode, debug_ranking=debug_ranking)
    except Exception as exc:
        logger.warning(f"RAG 检索失败: {exc}")
        return [], {"error": str(exc), "hits": [], "channel_counts": {},
                    "visibility": mode}

    hits = result.get("hits") or []
    for h in hits:
        score = h.get("final_score")
        if score is None:
            score = h.get("rerank_score")
        if score is None:
            score = h.get("rrf_score") or 0.0
        h["_score"] = float(score or 0.0)
    return hits, result


def build_rag_context(guides: List[dict], max_chars: int = 3000,
                      owner_user_id: Optional[str] = None,
                      mode: Optional[str] = None) -> str:
    """把检索到的攻略组装成可注入 system prompt 的参考文本。

    owner_user_id + mode：用于给每条攻略标注「我的攻略 / 公开攻略（作者）」
    与整体说明文字（公开模式下不能把别人的攻略说成用户自己的经历）。
    """
    if not guides:
        return ""
    header = MODE_CONTEXT_HEADER.get(mode or "", None)
    return _build_context_from_hits(guides, max_chars=max_chars,
                                    owner_user_id=owner_user_id,
                                    header=header)


def rag_status() -> Dict[str, Any]:
    """当前 RAG 服务配置摘要（便于调试/健康检查）。"""
    from services.rag.store import VECTOR_COLLECTIONS, GUIDE_INDEX
    return {
        "intent_model": RAG_INTENT_MODEL,
        "intent_skill": RAG_INTENT_SKILL,
        "history_keywords": len(HISTORY_KEYWORDS),
        "guide_index": GUIDE_INDEX,
        "vector_collections": VECTOR_COLLECTIONS,
        "decision_cache": len(_DECISION_CACHE),
    }
