"""Elasticsearch 客户端封装

支持两种本地形态（ES 8/9 安装后**默认开启安全**，必须用第 2 种或关闭安全）：

1. 关闭安全（开发最省事）：在 elasticsearch.yml 设 `xpack.security.enabled: false` 并重启，
   然后 ES_URL=http://localhost:9200
2. 开启安全（默认）：ES_URL=https://localhost:9200 + ES_USER/ES_PASSWORD，证书校验二选一：
   - ES_CA_CERTS=E:\\elasticsearch-9.5.3\\config\\certs\\http_ca.crt   （推荐，校验证书）
   - ES_VERIFY_CERTS=false                                            （跳过校验，仅本机开发用）

⚠ 常见坑：ES_URL 写成 http:// 而 ES 是 https → 服务端直接断开连接，客户端报
   "Remote end closed connection without response" / "Connection aborted"。
"""

import logging
import os
from typing import Any, Dict, List, Optional

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError

from services.env_bootstrap import env_file_hint, load_env

# ⚠ 必须在读取下面的环境变量之前执行
load_env()

logger = logging.getLogger(__name__)

ES_URL = os.getenv("ES_URL", "http://localhost:9200").strip()
ES_USER = os.getenv("ES_USER", "").strip()
ES_PASSWORD = os.getenv("ES_PASSWORD", "").strip()
ES_CA_CERTS = os.getenv("ES_CA_CERTS", "").strip()

_verify_raw = os.getenv("ES_VERIFY_CERTS", "").strip().lower()
if _verify_raw:
    ES_VERIFY_CERTS = _verify_raw in ("1", "true", "yes", "on")
elif ES_URL.startswith("https"):
    # 未显式配置时：给了 CA 就严格校验；只给 https 无 CA（自签证书）则默认跳过校验，避免直接连不上
    ES_VERIFY_CERTS = bool(ES_CA_CERTS)
else:
    ES_VERIFY_CERTS = True

GUIDE_INDEX = "travel_guides"
CHAT_INDEX = "chat_messages"
USER_INDEX = "users"

# 向量维度（根据 embedding 模型调整）
VECTOR_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))


def _get_client() -> Elasticsearch:
    kwargs: Dict[str, Any] = {"hosts": [ES_URL], "request_timeout": 30}
    if ES_USER and ES_PASSWORD:
        kwargs["basic_auth"] = (ES_USER, ES_PASSWORD)
    elif ES_URL.startswith("https"):
        logger.warning("ES_URL 为 https 但未配置 ES_USER/ES_PASSWORD，认证会失败（401）")

    if ES_URL.startswith("https"):
        if ES_CA_CERTS:
            kwargs["ca_certs"] = ES_CA_CERTS
        kwargs["verify_certs"] = ES_VERIFY_CERTS
        if not ES_VERIFY_CERTS:
            kwargs["ssl_show_warn"] = False
    return Elasticsearch(**kwargs)


_client: Optional[Elasticsearch] = None


def get_es() -> Elasticsearch:
    global _client
    if _client is None:
        _client = _get_client()
    return _client


def es_health() -> dict:
    """探测 ES 可用性，并针对常见连接问题给出可操作提示。"""
    try:
        client = get_es()
        info = client.info()
        return {
            "status": "ok",
            "version": info.get("version", {}).get("number", "unknown"),
            "url": ES_URL,
            "auth": bool(ES_USER and ES_PASSWORD),
        }
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        hint = ""
        if "security_exception" in low or "401" in msg or "unauthorized" in low:
            hint = ("认证失败：配置 ES_USER/ES_PASSWORD（用户通常为 elastic）。"
                    r"重置密码：E:\elasticsearch-9.5.3\bin\elasticsearch-reset-password.bat -u elastic -i")
        elif ES_URL.startswith("http://") and (
            "remote end closed" in low or "connection aborted" in low or "disconnected" in low
        ):
            hint = ("ES 8/9 默认开启安全（HTTPS）：把 .env 的 ES_URL 改成 https:// 并配置 "
                    "ES_USER/ES_PASSWORD（可配 ES_CA_CERTS 指向 config\\certs\\http_ca.crt），"
                    "或在 elasticsearch.yml 设 xpack.security.enabled: false 后重启 ES")
        return {"status": "error", "message": msg, "hint": hint,
                "url": ES_URL, "auth": bool(ES_USER and ES_PASSWORD)}


# ================================================================
# 索引管理
# ================================================================

GUIDE_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    },
    "mappings": {
        "properties": {
            # guide_id 必须是 keyword：它是文档主键，列表返回它、详情/评价/公开都按它查
            "guide_id": {"type": "keyword"},
            "user_id": {"type": "keyword"},
            "username": {"type": "keyword"},
            "nickname": {"type": "keyword"},
            "title": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}}
            },
            "destination": {
                "type": "keyword",
                "fields": {"text": {"type": "text"}}
            },
            "days": {"type": "integer"},
            "summary": {"type": "text"},
            "content": {"type": "text", "index": False},
            "content_json": {"type": "text", "index": False},
            "keywords": {
                "type": "keyword"
            },
            "embedding": {
                "type": "dense_vector",
                "dims": VECTOR_DIM,
                "index": True,
                "similarity": "cosine"
            },
            "rating": {"type": "integer"},
            "rating_text": {"type": "text"},
            "is_public": {"type": "boolean"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"}
        }
    }
}

CHAT_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    },
    "mappings": {
        "properties": {
            "user_id": {"type": "keyword"},
            "role": {"type": "keyword"},
            "content": {"type": "text"},
            "guide_refs": {"type": "keyword"},
            "created_at": {"type": "date"}
        }
    }
}

USER_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    },
    "mappings": {
        "properties": {
            "user_id": {"type": "keyword"},
            "username": {"type": "keyword"},
            "nickname": {"type": "keyword"},
            "password_hash": {"type": "keyword", "index": False},
            "created_at": {"type": "date"}
        }
    }
}


def ensure_indices():
    """创建所需索引（如不存在）"""
    client = get_es()
    index_map = {
        GUIDE_INDEX: GUIDE_MAPPING,
        CHAT_INDEX: CHAT_MAPPING,
        USER_INDEX: USER_MAPPING,
    }
    for name, body in index_map.items():
        try:
            if not client.indices.exists(index=name):
                client.indices.create(index=name, body=body)
                logger.info(f"Created index: {name}")
            else:
                logger.info(f"Index already exists: {name}")
        except Exception as e:
            logger.error(f"Index {name} init failed: {e}")

    _apply_new_mapping_fields(client, GUIDE_INDEX, GUIDE_MAPPING)


def _apply_new_mapping_fields(client, index: str, mapping: dict) -> None:
    """给已存在的索引补齐"新增字段"的映射（幂等，失败只告警）。

    背景：索引已存在时 create 会被跳过，新加的字段就会走动态映射
    （例如 guide_id 被当成 text，导致按 guide_id 精确查不到文档）。
    注意：已存在且类型冲突的字段无法原地改类型，只能靠查询兜底或重建索引。
    """
    wanted = (mapping.get("mappings") or {}).get("properties") or {}
    try:
        current = client.indices.get_mapping(index=index)
        existing = (current.get(index, {}).get("mappings") or {}).get("properties") or {}
        missing = {k: v for k, v in wanted.items() if k not in existing}
        if not missing:
            return
        client.indices.put_mapping(index=index, properties=missing)
        logger.info(f"{index} 补充字段映射: {sorted(missing)}")
    except Exception as e:
        logger.warning(f"{index} 字段映射补充失败（不影响查询兜底）: {e}")


# ================================================================
# 用户 CRUD
# ================================================================

def create_user(user_id: str, username: str, nickname: str, password_hash: str) -> dict:
    client = get_es()
    from datetime import datetime, timezone
    doc = {
        "user_id": user_id,
        "username": username,
        "nickname": nickname,
        "password_hash": password_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = client.index(index=USER_INDEX, id=user_id, document=doc, refresh="wait_for")
    return doc


def get_user_by_username(username: str) -> Optional[dict]:
    client = get_es()
    result = client.search(
        index=USER_INDEX,
        body={"query": {"term": {"username": username}}, "size": 1}
    )
    hits = result.get("hits", {}).get("hits", [])
    return hits[0]["_source"] if hits else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    client = get_es()
    try:
        result = client.get(index=USER_INDEX, id=user_id)
        return result["_source"]
    except NotFoundError:
        return None


# ================================================================
# 攻略 CRUD
# ================================================================

def _resolve_doc_id(client, guide_id: str) -> Optional[str]:
    """把 guide_id 解析成 ES 文档的 `_id`。

    历史原因：早期 `save_guide()` 不带 id 索引，ES 自动生成 `_id`，而文档里的
    `guide_id` 字段是另一个 UUID —— 于是"列表里能看见，点详情却 404"。
    这里先按 `_id` 直取，取不到再按 `guide_id` 字段反查，兼容已经存进去的老数据。
    """
    try:
        client.get(index=GUIDE_INDEX, id=guide_id, _source=False)
        return guide_id
    except Exception:
        pass
    try:
        res = client.search(index=GUIDE_INDEX, query={"bool": {
            "should": [
                # keyword 映射：精确命中
                {"term": {"guide_id": guide_id}},
                # 历史数据把 guide_id 动态映射成了 text：用 match_phrase 做分词后的短语匹配
                {"match_phrase": {"guide_id": guide_id}},
            ],
            "minimum_should_match": 1,
        }}, size=1)
        hits = res.get("hits", {}).get("hits", [])
        return hits[0]["_id"] if hits else None
    except Exception:
        return None


def save_guide(doc: dict) -> str:
    client = get_es()
    # 显式用 doc 里的 guide_id 作为 _id：保证"文档 _id == 文档里的 guide_id"，
    # 否则列表返回的 guide_id 在详情/评价/公开接口里都查不到。
    guide_id = doc.get("guide_id")
    result = client.index(index=GUIDE_INDEX, id=guide_id or None,
                          document=doc, refresh="wait_for")
    return result["_id"]


def get_guide(guide_id: str) -> Optional[dict]:
    client = get_es()
    try:
        doc_id = _resolve_doc_id(client, guide_id)
        if not doc_id:
            return None
        # 排除 embedding（1024 维向量），列表/详情接口都不需要，白白增加响应体积
        result = client.get(index=GUIDE_INDEX, id=doc_id, _source_excludes=["embedding"])
        source = result["_source"]
        source.setdefault("guide_id", doc_id)
        return source
    except Exception:
        return None


def update_guide(guide_id: str, updates: dict) -> bool:
    client = get_es()
    try:
        doc_id = _resolve_doc_id(client, guide_id)
        if not doc_id:
            logger.error(f"Update guide {guide_id} failed: 文档不存在")
            return False
        client.update(index=GUIDE_INDEX, id=doc_id, doc=updates, refresh="wait_for")
        return True
    except Exception as e:
        logger.error(f"Update guide {guide_id} failed: {e}")
        return False


def _guide_list_body(ownership_filter: list, keyword: Optional[str] = None,
                     page: int = 1, page_size: int = 20) -> dict:
    """攻略列表查询体：归属硬过滤 + 可选关键字（命中标题/目的地/关键词/摘要）。

    关键字用 multi_match + operator=and：中文会被 standard 分词器切成单字，
    用 and 可以保证"青岛"这类词不会因为只命中"青"就被召回。
    """
    bool_query: dict = {"filter": ownership_filter}
    kw = (keyword or "").strip()
    if kw:
        bool_query["must"] = [{
            "multi_match": {
                "query": kw,
                "fields": ["title^3", "destination^2", "keywords^2", "summary"],
                "operator": "and",
            }
        }]
    return {
        "query": {"bool": bool_query},
        "sort": [{"created_at": {"order": "desc"}}],
        "from": max(0, (page - 1) * page_size),
        "size": page_size,
        "_source": {"excludes": ["embedding"]},
    }


def list_user_guides(user_id: str, page: int = 1, page_size: int = 20,
                     keyword: Optional[str] = None) -> dict:
    """我的攻略列表（keyword 非空时按关键字过滤，total 为过滤后的数量）。"""
    client = get_es()
    result = client.search(
        index=GUIDE_INDEX,
        body=_guide_list_body([{"term": {"user_id": user_id}}],
                              keyword=keyword, page=page, page_size=page_size),
    )
    hits = result.get("hits", {}).get("hits", [])
    total = result.get("hits", {}).get("total", {}).get("value", 0)
    return {"items": [h["_source"] for h in hits], "total": total}


def list_public_guides(page: int = 1, page_size: int = 20,
                       keyword: Optional[str] = None) -> dict:
    """公开攻略列表（keyword 非空时按关键字过滤，total 为过滤后的数量）。"""
    client = get_es()
    result = client.search(
        index=GUIDE_INDEX,
        body=_guide_list_body([{"term": {"is_public": True}}],
                              keyword=keyword, page=page, page_size=page_size),
    )
    hits = result.get("hits", {}).get("hits", [])
    total = result.get("hits", {}).get("total", {}).get("value", 0)
    return {"items": [h["_source"] for h in hits], "total": total}


# ================================================================
# 聊天记录
# ================================================================

def save_chat_message(doc: dict) -> str:
    client = get_es()
    result = client.index(index=CHAT_INDEX, document=doc, refresh="wait_for")
    return result["_id"]


def get_chat_history(user_id: str, limit: int = 50) -> List[dict]:
    client = get_es()
    result = client.search(
        index=CHAT_INDEX,
        body={
            "query": {"term": {"user_id": user_id}},
            "sort": [{"created_at": {"order": "desc"}}],
            "size": limit,
        }
    )
    hits = result.get("hits", {}).get("hits", [])
    return [h["_source"] for h in reversed(hits)]
