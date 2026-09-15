# 云端部署审计（多用户场景）
===============================================================================

审计时间：2026-09（对照当前代码）
结论速览：

| 场景 | 是否可上线 | 说明 |
|------|-----------|------|
| 本机 / 内网单机、≤5 人同时用 | ✅ 可以 | 现有代码直接跑，注意「单 worker」约束 |
| 单台云服务器、小团队（≤20 人） | ⚠️ 补 5 项 P0 后可上线 | 见「P0 上线前必做」 |
| 多副本 / 水平扩容 / 公网多租户 | ❌ 暂不可 | 进程内状态（任务表、MCP 实例表、并发信号量）必须先外置到 Redis |

---

## 一、已经具备的多用户能力（本次及前序已实现）

| 能力 | 实现位置 | 说明 |
|------|---------|------|
| 账号体系 | `auth/`、`controller/auth_routes.py` | bcrypt 口令 + JWT（默认 72h） |
| 会话与消息隔离 | `services/memory_store.py` | PG 按 user_id 存取；跨用户访问抛 `PermissionError` |
| 同会话并发保护 | `services/memory_store.py :: acquire_session_lock` | Redis 分布式锁，90s 过期，避免同会话并发交错写 |
| RAG 归属过滤（三层） | `services/rag/retrieval.py` | ES `bool.filter` + Qdrant payload filter + PG 兜底；`own` / `own_or_public` / `public` / `all` |
| 聊天三模式 | `services/chat_service.py` | 普通 / 历史（仅自己）/ 公开（自己 + 他人公开） |
| 攻略列表与详情权限 | `controller/guide_routes.py`、`share_routes.py` | 列表按 user_id；详情「本人或已公开」；评价/公开仅本人 |
| 攻略搜索 | `services/es_client.py :: _guide_list_body` | 关键字 + 归属过滤，`total` 为过滤后数量 |
| 分析任务鉴权 + 归属校验 | `controller/summary_routes.py`、`services/summary_task.py` | **本次修复的越权漏洞**：原先接口无鉴权、`task_id` 仅 0–10000 可枚举 |
| task_id 不可枚举 | `functions/get_intent.py :: _new_task_id` | 48 位随机（≈2.8e14），仍在 JS 安全整数内 |
| 小红书一人一实例 | `services/xhs_manager.py` | 每用户独立工作目录（cookies 隔离）+ 独立端口；未登录禁止生成攻略 |
| 日志与主应用分离 | `log_viewer.py` | 独立端口 + 固定口令；主应用日志接口默认关闭 |
| 上传前密钥自查 | `check_secrets.py` | 也可挂 pre-commit |

---

## 二、P0：公网上线前必须处理

### 1. 单进程状态（最关键）

以下都是**进程内**状态，多 worker / 多副本会直接出错：

| 状态 | 位置 | 多副本时的后果 |
|------|------|---------------|
| 分析任务注册表 `_TASKS` | `services/summary_task.py` | 订阅请求落到另一个 worker → 任务「不存在」，进度丢失；同一任务可能被分析两次 |
| 小红书实例表 `_instances` | `services/xhs_manager.py` | 重复起实例、端口冲突、实例停不掉 |
| MCP 并发信号量 | `services/mcp_concurrency.py` | 并发上限乘以副本数，实际超卖 |

**做法（任选）**
- 短期：**保持单 worker 单副本**（`uvicorn` 不加 `--workers`），并在编排层禁止横向扩容；
  启动时会检测 `WEB_CONCURRENCY>1` 并打 ERROR 日志（`main.py :: _warn_insecure_defaults`）。
- 中期（推荐）：把「任务事件流」搬到 Redis Stream/List（`task:{id}:events` + 发布订阅），
  注册表只留本地缓存；小红书实例改为「谁启动谁负责 + 心跳续约」，端口分配放 Redis `INCR`。

### 2. 反向代理 + HTTPS

- Nginx/Caddy 终止 TLS，转发到 `127.0.0.1:8088`（后端只监听环回地址）。
- SSE 必须关缓冲，否则进度会「攒着一次性吐出来」：
  ```nginx
  location /api/ {
      proxy_pass http://127.0.0.1:8088;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_buffering off;           # SSE/流式必须
      proxy_cache off;
      proxy_read_timeout 600s;       # 分析任务可能跑几分钟
      proxy_send_timeout 600s;
  }
  ```
- `CORS_ORIGINS` 收敛成真实前端域名（默认只放 localhost:5173）。

### 3. 密钥与配置

- `JWT_SECRET_KEY` 必须是随机长串（生产环境启动时已 ERROR 告警；建议直接拒绝启动）。
- `.env` 不要进镜像：用平台的环境变量/密钥管理（K8s Secret、Docker secrets、云厂商参数存储）。
- `.env.example` 只能有占位值（已修正过一次真实 Key 泄露）。
- 小红书 `cookies.json` 属于高价值凭据：放在持久卷、权限收紧，且**不要**跟代码一起备份到公开位置。

### 4. 限流与配额（防刷 = 防账单爆炸）

当前**没有任何限流**：登录接口可被暴力尝试，LLM/Embedding/Rerank 接口可被刷。
建议：
- Nginx `limit_req`（按 IP）+ 应用层按 user_id 的令牌桶（Redis）。
- 登录失败 ≥5 次锁定 15 分钟（`controller/auth_routes.py` 加计数）。
- 每用户每日「生成攻略次数 / 聊天条数」配额（分析一次会调很多次 LLM 与小红书接口）。
- 小红书实例上限已有（`XHS_MAX_INSTANCES`，默认 3），超出时返回排队提示。

### 5. 健康检查、备份与日志轮转

- `/api/health` 目前只查 ES 与进程存活。生产建议区分 liveness / readiness，readiness 里全量检查
  PG / Redis / Qdrant / ES，任一不可用就从负载均衡摘除。
- PG：每日 `pg_dump` + WAL 归档；ES：索引快照到对象存储；Qdrant：快照或复制卷。
- `logs/` 会无限增长（`logs/backend.log`、`logs/xhs-mcp-*.log`）→ 用 logrotate 或 Docker 的
  `max-size` 限制；长期趋势进 Loki/ELK。

---

## 三、P1：建议尽快做

| 项 | 现状 | 建议 |
|----|------|------|
| 数据库迁移 | 启动时 `pg_ensure_schema()` 直接 DDL，无版本管理 | 引入 Alembic；初始化 DDL 变成一次性 Job，避免多副本并发建表 |
| 指标与追踪 | 只有文件日志 | `/metrics`（Prometheus）+ 关键耗时打点（意图识别/检索/重排/LLM/入库） |
| ES 写入压力 | 每次写 `refresh=wait_for` | 高并发下改 `refresh=false` + 定时 `_refresh`，或按用户分索引 |
| 账号体系 | 只有口令登录 | 加邮箱/手机验证、找回密码、管理员后台（封禁/配额调整） |
| 前端部署 | Vite dev server（仅开发用） | `npm run build` 产物交给 Nginx/静态托管，`VITE_LOG_VIEWER_URL` 指向日志服务 |
| 内容合规 | 小红书笔记正文/图片会进 LLM | 明确留存期限与聚合方式，必要时脱敏；遵守平台条款 |
| 成本可见性 | 无 | 统计每用户 token 消耗与外部 API 调用量 |

---

## 四、小红书 MCP：云端的关键限制

`xiaohongshu-mcp` / `xiaohongshu-login` 是**本地可执行文件 + 真浏览器**的形态：

| 限制 | 说明 |
|------|------|
| 平台 | 官方 releases 有 Windows / Linux / macOS 构建；当前项目用的是 Windows exe |
| 登录 | 需要用户**扫码**：Windows 下是弹出浏览器窗口；云端无桌面，必须改成"把二维码图片返回给前端" |
| 资源 | 每用户一个实例 ≈ 一个 Chromium（几百 MB 内存）；默认上限 3 |
| 会话 | cookies 落在实例工作目录，属敏感凭据 |

→ 云端方案与容量估算见 **`docs/xhs-multi-user.md`**。

---

## 五、结论与行动清单

**当前状态**：单机/内网、单副本、≤5 人并发使用是**可用**的；代码层面的多用户数据隔离
（攻略、聊天记忆、RAG 检索、分析任务）已经做完并验证过。

**要放到公网给多人用，按顺序做**：

1. 反代 + HTTPS + SSE 关缓冲（半天）
2. 明确单副本约束（或把任务状态迁 Redis）（0.5 天 / 2 天）
3. 换掉默认 JWT 密钥、收敛 CORS、密钥走平台注入（0.5 天）
4. 限流 + 登录失败锁定 + 每用户配额（1 天）
5. readiness 健康检查 + 备份 + 日志轮转（1 天）
6. 前端静态托管 + 小红书云端方案（见专项文档）
7. Alembic 迁移 + 指标（1–2 天）
