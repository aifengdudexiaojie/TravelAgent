"""FastAPI 后端 - 旅行规划 API + 小红书 MCP 集成"""

import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from mock_data import MOCK_TRAVEL_PLAN
from controller import api_router

app = FastAPI(title="Travel Planner API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================
# 注册 Controller 路由
#   POST /api/intent/recognize  - 意图识别（一次输出）
#   POST /api/plan              - 完整旅行规划
# ================================================================
app.include_router(api_router)


class QueryRequest(BaseModel):
    query: str


class SearchRequest(BaseModel):
    location: str
    keywords: list[str] | None = None
    max_results: int = 15


# ================================================================
# 基础端点
# ================================================================


@app.get("/api/health")
def health_check():
    return {"status": "ok", "timestamp": int(time.time() * 1000)}


# ================================================================
# 小红书 MCP 搜索（需先启动 xiaohongshu-mcp 服务）
# ================================================================


@app.get("/api/mcp/status")
def mcp_status():
    try:
        import httpx
        resp = httpx.get("http://localhost:18060/health", timeout=5)
        return {"status": "ok" if resp.status_code == 200 else "offline"}
    except Exception:
        return {"status": "offline", "message": "MCP 服务未启动"}


@app.post("/api/mcp/search")
def search_xiaohongshu(req: SearchRequest):
    from xiaohongshu_mcp_client import search_travel_posts
    posts = search_travel_posts(
        location=req.location,
        keywords=req.keywords,
        max_results=req.max_results,
    )
    return {"location": req.location, "total": len(posts), "posts": posts}


# ================================================================
# 兼容旧端点（后续可删除）
# POST /api/travel-plan — 保留用于兼容
# ================================================================


@app.post("/api/travel-plan")
def travel_plan_legacy(req: QueryRequest):
    time.sleep(2)
    return MOCK_TRAVEL_PLAN


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088)
