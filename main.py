"""FastAPI 后端 - 旅行规划 API + 小红书 MCP 集成 + RAG + 用户系统"""

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 ES、PostgreSQL、Qdrant 与 Redis"""
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
    yield
    await memory_store.close()


app = FastAPI(
    title="AI 旅行规划助手",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
