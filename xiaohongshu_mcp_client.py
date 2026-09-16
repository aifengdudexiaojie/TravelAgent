"""
小红书 MCP 客户端 - 封装 xiaohongshu-mcp 的 Streamable HTTP 接口

依赖: pip install httpx

使用流程:
  1. 启动 xiaohongshu-mcp 服务 (详见 README-MCP.md)
  2. 扫码登录
  3. 调用本客户端搜索

注意: xiaohongshu-mcp 使用 MCP Streamable HTTP 协议，
      每次请求需以 batch 形式发送 [initialize, initialized, tools/call]
"""

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MCP_URL = "http://localhost:18060/mcp"


def _batch_call(method: str, params: dict | None = None,
                base_url: str = MCP_URL,
                max_retries: int = 2,
                timeout: float = 120) -> Any:
    """
    发送 MCP batch 请求（initialize + initialized + target_method）

    xiaohongshu-mcp 要求在同一个 HTTP 请求中按顺序发送:
      1. initialize (含 id)
      2. notifications/initialized (不含 id)
      3. 实际要调用的方法 (含 id)

    Args:
        method: MCP 方法名
        params: 方法参数
        base_url: MCP 服务地址
        max_retries: 失败重试次数（MCP 是浏览器操作，偶发超时正常）
        timeout: 单次 HTTP 超时秒数。⚠️ 查登录状态这类"每几秒轮询一次"的调用
                 必须传小值（见 services/xhs_manager.STATUS_TIMEOUT）：MCP 在忙
                 浏览器操作时可能长时间不返回，默认 120s 会把状态接口一起拖死，
                 前端表现就是"页面一直没反应"。
    """
    payload = [
        {"jsonrpc": "2.0", "id": "1", "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "xiaohongshu-client", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "2", "method": method, "params": params or {}},
    ]

    import time as _time
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(
                base_url, json=payload,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=timeout,
            )
            resp.raise_for_status()
            responses = resp.json()
            break  # 成功则跳出重试循环
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"无法连接到 xiaohongshu-mcp 服务 ({base_url})\n"
                "请先启动服务: cd backend && python start_mcp.py"
            ) from e
        except httpx.TimeoutException as e:
            last_err = e
            print(f"⚠️ MCP 请求超时（第 {attempt+1}/{max_retries+1} 次）: {method}", flush=True)
            if attempt < max_retries:
                _time.sleep(2)  # 重试前稍等
                continue
            raise TimeoutError(
                f"请求超时（重试 {max_retries} 次后仍失败），请检查 xiaohongshu-mcp 服务状态"
            ) from e
        except httpx.HTTPError as e:
            last_err = e
            print(f"⚠️ MCP 请求失败（第 {attempt+1}/{max_retries+1} 次）: {method} - {e}", flush=True)
            if attempt < max_retries:
                _time.sleep(1)
                continue
            raise

    # 取最后一个有 id 的响应（实际方法调用的结果）
    # notifications/initialized 无响应，所以 data[1] 就是目标
    target = responses[-1] if len(responses) > 1 else responses[0]
    if "error" in target:
        err = target["error"]
        raise RuntimeError(f"MCP 错误: {err.get('message', str(err))}")

    # 提取 result.content[] 中的文本
    result = target.get("result", {})
    texts = []
    for c in result.get("content", []):
        if c.get("type") == "text":
            texts.append(c["text"])
    raw = "\n".join(texts)

    # 自动尝试解析 JSON
    if raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


def _batch_call_content(method: str, params: dict | None = None,
                        base_url: str = MCP_URL,
                        max_retries: int = 2,
                        timeout: float = 120) -> list[dict]:
    """同 `_batch_call`，但返回 MCP 的 **原始 content 列表**（text / image 都在里面）。

    为什么需要它：登录二维码不是文本，而是 MCP 返回的
    `{"type": "image", "mimeType": "image/png", "data": "<base64>"}` 片段，
    只取 text 会丢掉图片。见 `get_login_qrcode()`。
    """
    payload = [
        {"jsonrpc": "2.0", "id": "1", "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "xiaohongshu-client", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "2", "method": method, "params": params or {}},
    ]

    import time as _time
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(
                base_url, json=payload,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=timeout,
            )
            resp.raise_for_status()
            responses = resp.json()
            break
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"无法连接到 xiaohongshu-mcp 服务 ({base_url})"
            ) from e
        except httpx.TimeoutException as e:
            if attempt < max_retries:
                _time.sleep(2)
                continue
            raise TimeoutError(f"请求超时（重试 {max_retries} 次后仍失败）") from e
        except httpx.HTTPError:
            if attempt < max_retries:
                _time.sleep(1)
                continue
            raise

    target = responses[-1] if len(responses) > 1 else responses[0]
    if "error" in target:
        err = target["error"]
        raise RuntimeError(f"MCP 错误: {err.get('message', str(err))}")
    return list(target.get("result", {}).get("content", []))


def get_login_qrcode(base_url: str = MCP_URL) -> dict:
    """获取登录二维码。

    返回：{"text": 提示语, "image_base64": "...", "mime": "image/png", "expires_at": ISO|None}

    这是"云服务器上让用户自己登录"的正解：二维码由 MCP 生成，后端转成 data URL 给前端展示，
    用户用手机扫一下即可 —— 不需要桌面环境、不需要弹浏览器、也不需要用户碰 cookies 文件。
    """
    import re
    from datetime import datetime, timedelta, timezone

    contents = _batch_call_content("tools/call", {"name": "get_login_qrcode"},
                                   base_url=base_url, max_retries=0)
    out: dict = {"text": "", "image_base64": "", "mime": "image/png", "expires_at": None}
    for c in contents:
        ctype = c.get("type")
        if ctype == "text":
            out["text"] = (out["text"] + "\n" + (c.get("text") or "")).strip()
        elif ctype == "image":
            out["image_base64"] = c.get("data") or ""
            out["mime"] = c.get("mimeType") or "image/png"

    # 提示语形如「请用小红书 App 在 2026-09-16 15:20:45 前扫码登录 👇」，把过期时间解析出来给前端倒计时
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", out["text"] or "")
    if m:
        try:
            dt_local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            out["expires_at"] = dt_local.replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
        except ValueError:
            pass
    if not out["image_base64"]:
        out["message"] = out["text"] or "未取到二维码"
    return out


# ================================================================
# 工具函数
# ================================================================


def check_login_status() -> dict:
    """检查小红书登录状态"""
    result = _batch_call("tools/call", {"name": "check_login_status"})
    return result if isinstance(result, dict) else {"raw": str(result)}


def search_feeds(keyword: str, sort: str = "综合",
                 note_type: str = "不限", limit: int = 10) -> list[dict]:
    """
    搜索小红书笔记

    Args:
        keyword: 搜索关键词，如 "成都旅游攻略"
        sort: 排序方式 - 综合 | 最新 | 最多点赞 | 最多评论 | 最多收藏
        note_type: 笔记类型 - 不限 | 视频 | 图文
        limit: 返回条数

    Returns:
        [{display_title, liked_count, collected_count, note_id, desc, ...}, ...]
    """
    result = _batch_call("tools/call", {
        "name": "search_feeds",
        "arguments": {
            "keyword": keyword,
            "filters": {
                "sort_by": sort,
                "note_type": note_type,
            },
        },
    })
    feeds = result if isinstance(result, list) else []
    return feeds[:limit]


def get_feed_detail(note_id: str, xsec_token: str) -> dict:
    """获取笔记详情"""
    result = _batch_call("tools/call", {
        "name": "get_feed_detail",
        "arguments": {"feed_id": note_id, "xsec_token": xsec_token},
    })
    return result if isinstance(result, dict) else {"raw": str(result)}


def list_feeds() -> list[dict]:
    """获取首页推荐"""
    result = _batch_call("tools/call", {"name": "list_feeds"})
    return result if isinstance(result, list) else []


# ================================================================
# 旅游 Agent 专用函数
# ================================================================


def search_travel_posts(
    location: str,
    keywords: list[str] | None = None,
    max_results: int = 15,
) -> list[dict]:
    """
    搜索某个地点的旅游相关小红书帖子（多关键词去重）

    Args:
        location: 目的地 (如 "成都", "宜兴")
        keywords: 搜索关键词组合，默认 ["旅游攻略", "景点推荐", "美食推荐"]
        max_results: 每个关键词最多取多少条

    Returns:
        去重后的帖子列表 [{title, likes, note_id, search_keyword, ...}, ...]
    """
    if keywords is None:
        keywords = ["旅游攻略", "景点推荐", "美食推荐", "游玩攻略"]

    seen = set()
    all_posts = []

    for kw in keywords:
        query = f"{location}{kw}"
        try:
            feeds = search_feeds(query, limit=5)
            for f in feeds:
                nid = f.get("note_id", "")
                if nid and nid not in seen:
                    seen.add(nid)
                    f["search_keyword"] = query
                    f["location"] = location
                    all_posts.append(f)
        except Exception as e:
            logger.warning("搜索 '%s' 失败: %s", query, e)

    return all_posts
