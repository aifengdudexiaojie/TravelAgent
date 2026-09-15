"""Embedding 服务 - 默认使用阿里云百炼 qwen3.7-text-embedding-flash（1024 维）

两种模式（二选一）：
1. HTTP API（默认）：任何 OpenAI 兼容接口。默认指向阿里云百炼 DashScope：
       base_url = https://dashscope.aliyuncs.com/compatible-mode/v1
       model    = qwen3.7-text-embedding-flash
       dims     = 1024（该模型支持 256/512/768/1024，1024 为默认）
   可选：使用业务空间专属域名（更快更稳）
       https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
2. 本地模型：设置 USE_LOCAL_EMBEDDING=true（需 sentence-transformers）

相关环境变量
------------
    DASHSCOPE_API_KEY / EMBEDDING_API_KEY   百炼 API Key（任一即可）
    EMBEDDING_BASE_URL                      默认百炼 compatible-mode
    EMBEDDING_MODEL                         默认 qwen3.7-text-embedding-flash
    EMBEDDING_DIM                           默认 1024（必须与 Qdrant/ES 映射一致）
    EMBEDDING_BATCH_SIZE                    单次请求条数，默认 10（百炼同步接口有行数上限）
    EMBEDDING_SEND_DIMENSIONS               是否传 dimensions 参数，默认 true
    EMBEDDING_TIMEOUT                       单次请求超时秒数，默认 30
"""

import logging
import os
from typing import List, Optional

from services.env_bootstrap import env_file_hint, load_env

# ⚠ 必须在下面的模块级环境变量读取之前执行，否则 .env 里的配置会被静默忽略
load_env()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen3.7-text-embedding-flash"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "").strip() or DEFAULT_BASE_URL
EMBEDDING_API_KEY = (
    os.getenv("EMBEDDING_API_KEY", "").strip()
    or os.getenv("DASHSCOPE_API_KEY", "").strip()
)
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
EMBEDDING_SEND_DIMENSIONS = os.getenv("EMBEDDING_SEND_DIMENSIONS", "true").lower() == "true"
EMBEDDING_TIMEOUT = float(os.getenv("EMBEDDING_TIMEOUT", "30"))

USE_LOCAL_MODEL = os.getenv("USE_LOCAL_EMBEDDING", "false").lower() == "true"
LOCAL_MODEL_NAME = os.getenv("LOCAL_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

_local_model = None
_client = None
_dim_warned = False


def _get_local_model():
    global _local_model
    if _local_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _local_model = SentenceTransformer(LOCAL_MODEL_NAME)
            logger.info(f"Loaded local embedding model: {LOCAL_MODEL_NAME}")
        except ImportError:
            raise RuntimeError(
                "sentence-transformers 未安装。请运行: pip install sentence-transformers"
            )
    return _local_model


def _get_client():
    """懒加载 OpenAI 兼容客户端（复用连接）。"""
    global _client
    if _client is None:
        if not EMBEDDING_API_KEY:
            raise RuntimeError(
                "Embedding 未配置。请设置 DASHSCOPE_API_KEY（或 EMBEDDING_API_KEY），"
                "或设置 USE_LOCAL_EMBEDDING=true 使用本地模型"
            )
        from openai import OpenAI
        _client = OpenAI(
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_BASE_URL,
            timeout=EMBEDDING_TIMEOUT,
        )
        logger.info(
            "Embedding 客户端就绪：model=%s base_url=%s dim=%s",
            EMBEDDING_MODEL, EMBEDDING_BASE_URL, EMBEDDING_DIM,
        )
    return _client


def _request_kwargs(texts: List[str], with_dimensions: bool) -> dict:
    kwargs = {
        "input": texts,
        "model": EMBEDDING_MODEL,
        "encoding_format": "float",
    }
    if with_dimensions:
        kwargs["dimensions"] = EMBEDDING_DIM
    return kwargs


def _warn_dimension_mismatch(got: int) -> None:
    global _dim_warned
    if got != EMBEDDING_DIM and not _dim_warned:
        _dim_warned = True
        logger.warning(
            "Embedding 实际维度 %s 与 EMBEDDING_DIM=%s 不一致！"
            "请统一 .env 的 EMBEDDING_DIM，并重建向量集合（Qdrant 集合维度必须匹配）。",
            got, EMBEDDING_DIM,
        )


def embed_texts(texts: List[str]) -> List[List[float]]:
    """批量向量化（自动分批；单条失败不回退静默，由调用方决定降级策略）。"""
    if not texts:
        return []

    if USE_LOCAL_MODEL:
        model = _get_local_model()
        embeddings = model.encode(texts, normalize_embeddings=True)
        vectors = [e.tolist() for e in embeddings]
        if vectors:
            _warn_dimension_mismatch(len(vectors[0]))
        return vectors

    client = _get_client()
    vectors: List[List[float]] = []
    batch_size = max(1, EMBEDDING_BATCH_SIZE)

    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        try:
            resp = client.embeddings.create(**_request_kwargs(chunk, EMBEDDING_SEND_DIMENSIONS))
        except Exception as exc:
            # 部分网关/模型不支持 dimensions 参数：去掉后重试一次
            if EMBEDDING_SEND_DIMENSIONS and "dimension" in str(exc).lower():
                logger.warning("服务端不支持 dimensions 参数，改为使用模型默认维度重试：%s", exc)
                resp = client.embeddings.create(**_request_kwargs(chunk, False))
            else:
                raise
        # 按 index 排序，保证返回顺序与输入一致
        data = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        vectors.extend([d.embedding for d in data])

    if vectors:
        _warn_dimension_mismatch(len(vectors[0]))
    return vectors


def embed_text(text: str) -> List[float]:
    """将单条文本向量化。"""
    vectors = embed_texts([text])
    if not vectors:
        raise RuntimeError("Embedding 返回为空")
    return vectors[0]


def embedding_dim() -> int:
    """返回当前配置的向量维度（供 Qdrant/ES 建集合与映射使用）。"""
    return EMBEDDING_DIM


def embedding_signature() -> str:
    """当前向量空间标识：`mode:model:dim`。

    入库时把它记录到 PostgreSQL.rag_meta，查询时比对——防止"入库用 A 模型、
    查询用 B 模型"这种**不报错但检索质量静默崩坏**的情况（例如误开
    USE_LOCAL_EMBEDDING 导致查询向量换成 bge-small 512 维）。
    """
    if USE_LOCAL_MODEL:
        return f"local:{LOCAL_MODEL_NAME}:{EMBEDDING_DIM}"
    return f"api:{EMBEDDING_MODEL}:{EMBEDDING_DIM}"


def api_key_source() -> str:
    """API Key 实际来自哪个环境变量（便于排查"配了却没生效"）。"""
    if os.getenv("EMBEDDING_API_KEY", "").strip():
        return "EMBEDDING_API_KEY"
    if os.getenv("DASHSCOPE_API_KEY", "").strip():
        return "DASHSCOPE_API_KEY"
    return "（未配置）"


def describe() -> dict:
    """当前 embedding 配置摘要（用于启动日志/自检输出）。"""
    return {
        "mode": "local" if USE_LOCAL_MODEL else "api",
        "model": LOCAL_MODEL_NAME if USE_LOCAL_MODEL else EMBEDDING_MODEL,
        "base_url": "" if USE_LOCAL_MODEL else EMBEDDING_BASE_URL,
        "dim": EMBEDDING_DIM,
        "batch_size": EMBEDDING_BATCH_SIZE,
        "send_dimensions": EMBEDDING_SEND_DIMENSIONS and not USE_LOCAL_MODEL,
        "api_key_set": bool(EMBEDDING_API_KEY),
        "api_key_from": api_key_source(),
        "env_file": env_file_hint(),
    }
