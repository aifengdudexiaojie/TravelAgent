# ================================================================================
# 后端镜像（FastAPI + uvicorn）
#
# ⚠️ 单副本部署：分析任务注册表与小红书实例表都在进程内（见 docs/deployment-audit.md），
#    不要给 uvicorn 加 --workers 或横向扩容，除非先把这些状态迁到 Redis。
#
# 构建：docker build -t travel-agent-api .
# 运行：docker run --env-file .env -p 8088:8088 travel-agent-api
# ================================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 时区与健康检查所需的 curl
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用构建缓存
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 再拷代码（.dockerignore 已排除 .env / .venv / node_modules / 日志 / MCP 二进制）
COPY . .

# 日志目录（run_backend.py 会写这里；也可挂卷持久化）
RUN mkdir -p /app/logs

EXPOSE 8088 8099

# 健康检查：容器内探活（readiness 建议由编排层调 /api/health）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8088/api/health || exit 1

# 默认启动后端；日志查看器用同一个镜像跑 log_viewer.py（见 docker-compose.yml）
CMD ["python", "run_backend.py"]
