"""FastAPI 后端 - 旅行规划 API + 小红书 MCP 集成 + RAG + 用户系统"""

import asyncio
import os
import time
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from controller import api_router
from services import memory_store
from services.es_client import ensure_indices, es_health
from services.mcp_concurrency import get_mcp_status, check_xhs_login_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
_is_prod = APP_ENV in ("production", "prod")

# 允许的前端来源：生产必须显式配置，不要用 "*"（尤其是带着 Cookie/Authorization 的时候）
_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()]


def _warn_insecure_defaults() -> None:
    """把"上线前必须改"的默认值在启动日志里说清楚（而不是等出事故）。"""
    secret = os.getenv("JWT_SECRET_KEY", "")
    if not secret or "change" in secret.lower():
        level = logger.error if _is_prod else logger.warning
        level("JWT_SECRET_KEY 仍是默认值：任何人都能伪造登录态，上线前必须换成随机长字符串")
    if "*" in CORS_ORIGINS:
        logger.warning("CORS_ORIGINS 含 *：请收敛为真实前端域名")
    workers = os.getenv("WEB_CONCURRENCY") or os.getenv("UVICORN_WORKERS") or "1"
    try:
        if int(workers) > 1:
            logger.error(
                "检测到多 worker（%s）：分析任务注册表与小红书 MCP 实例都是**进程内**状态，"
                "多 worker 会导致任务丢失/重复起实例。云端请保持单 worker，"
                "或者把任务状态迁到 Redis 后再水平扩容。", workers)
    except ValueError:
        pass
    if os.getenv("BACKEND_LOG_ENDPOINT", "false").lower() not in ("1", "true", "yes", "on"):
        logger.info("主应用日志接口已关闭（安全默认）：请用独立日志服务 python log_viewer.py（默认 :8099）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 ES、PostgreSQL、Qdrant 与 Redis；关闭时回收小红书 MCP 实例"""
    _warn_insecure_defaults()
    try:
        ensure_indices()
        logger.info("ES 索引初始化完成")
    except Exception as e:
        logger.warning(f"ES 初始化失败（服务仍可启动）: {e}")
    try:
        await memory_store.initialize()
        logger.info("长短期记忆存储初始化完成")
    except Exception as e:
        logger.warning(f"记忆存储初始化失败（服务仍可启动）: {e}")

    # 小红书 MCP 空闲实例回收（多用户模式下每个用户一个实例，需要及时释放内存）
    reaper = asyncio.create_task(_reap_xhs_instances())

    yield

    reaper.cancel()
    try:
        from services import xhs_manager
        await asyncio.to_thread(xhs_manager.stop_all)
    except Exception as e:
        logger.warning(f"回收小红书实例失败: {e}")
    await memory_store.close()


async def _reap_xhs_instances() -> None:
    from services import xhs_manager
    while True:
        try:
            await asyncio.sleep(300)
            killed = await asyncio.to_thread(xhs_manager.reap_idle)
            if killed:
                logger.info("回收空闲的小红书 MCP 实例 %d 个", killed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"回收小红书实例出错: {e}")


app = FastAPI(
    title="AI 旅行规划助手",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


class QueryRequest(BaseModel):
    query: str


class SearchRequest(BaseModel):
    location: str
    keywords: list[str] | None = None
    max_results: int = 15


@app.get("/api/health")
def health_check():
    es = es_health()
    return {
        "status": "ok",
        "timestamp": int(time.time() * 1000),
        "elasticsearch": es,
    }


@app.get("/api/mcp/status")
def mcp_status():
    return {
        "concurrency": get_mcp_status(),
        "xiaohongshu": check_xhs_login_status(),
    }


@app.post("/api/mcp/search")
def search_xiaohongshu(req: SearchRequest):
    from xiaohongshu_mcp_client import search_travel_posts
    posts = search_travel_posts(
        location=req.location,
        keywords=req.keywords,
        max_results=req.max_results,
    )
    return {"location": req.location, "total": len(posts), "posts": posts}


@app.post("/api/travel-plan")
def travel_plan_legacy(req: QueryRequest):
    from mock_data import MOCK_TRAVEL_PLAN
    time.sleep(2)
    return MOCK_TRAVEL_PLAN


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088)
