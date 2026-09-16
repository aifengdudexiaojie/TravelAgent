# 🤖 AI 旅行规划助手 v2.0

基于**大模型多 Agent 架构 + RAG 检索增强**的智能旅游攻略系统。集成 Elasticsearch 向量搜索、用户认证、旅游分享社区等功能。

## ✨ 核心特性

### 用户系统
- 用户名 + 密码注册/登录（JWT 鉴权）
- 用户中心：查看历史攻略、统计数据

### 智能聊天（RAG 集成）
- 旅游限定聊天：自动识别旅游相关问题
- RAG 检索增强：自动搜索历史攻略作为参考
- 向量 + 关键词混合搜索（Elasticsearch）
- 聊天记录持久化

### 旅游攻略生成
- 自然语言意图识别
- 小红书数据采集（xiaohongshu-mcp）
- 多 Agent 并发分析（总结/花费/注意事项/时长）
- 高德地图地理编码
- 攻略自动向量化存储

### 旅游分享社区
- 用户可公开自己的攻略
- 需先评价攻略后才可公开
- 浏览他人分享的攻略
- 评分 + 评价文字

### 并发控制
- 小红书 MCP 信号量控制（可配置最大并发数）
- 登录状态检测 + 运行时提示

## 🛠️ 技术栈

| 层次 | 技术 |
|------|------|
| 前端 | Vue 3 + TypeScript + Vite + Tailwind CSS + Pinia + Vue Router |
| 后端 | Python + FastAPI + uvicorn |
| 大模型 | DeepSeek / Kimi（OpenAI 兼容协议） |
| 数据源 | xiaohongshu-mcp（小红书浏览器自动化） |
| 数据库 | Elasticsearch 8.x（向量 + 全文 + 关键词） |
| 向量化 | OpenAI Embedding API / sentence-transformers（本地） |
| 认证 | JWT（python-jose + passlib/bcrypt） |
| 地理编码 | 高德地图 Geocoding API |
| 缓存 | Redis（会话锁 + task_id 管理） |
| 通信 | REST + SSE（Server-Sent Events） |

## 📁 目录结构

```
TravelAgent/
├── main.py                  # FastAPI 入口
├── run_backend.py           # 启动后端（控制台 + logs/backend.log 双写）
├── log_viewer.py            # 独立日志查看服务（单独端口 + .env 里的固定口令）
├── check_secrets.py         # 上传前密钥/凭据自查
├── check_repo.py            # 推送前仓库完整性自查（防 .gitignore 误伤源码）
├── fix_guide_owner.py       # 存量攻略归属修复（默认 dry-run）
├── ingest_by_es_transfer.py # CLI：攻略入库
├── rag_query.py             # CLI：RAG 检索查询
├── seed_rag_test_data.py    # 示例攻略数据（RAG 演示/回归用）
├── docker-compose.yml       # 依赖服务：PG / Qdrant / Redis / ES / Kibana
├── .env.example             # 环境变量模板（只放占位值）
├── requirements.txt         # 运行依赖
├── requirements-dev.txt     # 开发/测试依赖（pytest）
├── pytest.ini               # 单测配置（不会收集 test/manual/）
├── auth/                    # JWT 认证模块
├── models/                  # Pydantic 数据模型
├── services/                # 业务服务层
│   ├── es_client.py         # Elasticsearch 客户端 + 索引管理 + 攻略列表/搜索
│   ├── embedding.py         # 向量化服务
│   ├── rag_service.py       # RAG 意图判定 + 检索 + 上下文（三种模式）
│   ├── summary_task.py      # 攻略分析后台任务（进度可重放）
│   ├── chat_service.py      # 聊天 + RAG 集成
│   ├── memory_store.py      # 长短期记忆（PG + Qdrant + Redis）
│   └── rag/                 # RAG 栈：transform / ingest / store / retrieval / rerank / validate
├── controller/              # API 路由
│   ├── auth_routes.py       # 认证 API
│   ├── chat_routes.py       # 聊天 API（chat_mode: normal/history/public）
│   ├── summary_routes.py    # 分析任务订阅（SSE）+ 快照 + 取消
│   ├── guide_routes.py      # 攻略 API（含关键字搜索）
│   ├── share_routes.py      # 分享 API（公开列表 + 搜索 + 评价/公开）
│   ├── user_routes.py       # 用户中心 API
│   ├── intent_routes.py     # 意图识别
│   └── dev_routes.py        # 后端日志实时流（开发调试）
├── test/                    # 单元测试（不依赖外部服务）
│   ├── test_auth.py
│   ├── test_chat_service.py
│   └── manual/              # 手动/联调脚本（pytest 不收集，会真调 LLM/MCP）
├── agents/                  # Agent 框架
├── llm_grateway/            # LLM 统一网关
├── functions/               # 核心流程（意图、帖子分析、总结）
├── skills/                  # Agent 提示词
├── utils/                   # 工具函数
├── docs/                    # 文档：rag-code-map.md / deployment-runbook.md / deployment-audit.md / xhs-multi-user.md
├── deploy/                  # 部署物料：nginx.conf / systemd 单元 / backup.sh / check.sh
├── logs/                    # 运行日志（已 gitignore）
├── xiaohongshu_mcp_client.py
├── start_mcp.py
└── vue/                     # 前端
    └── src/
        ├── router/          # Vue Router
        ├── stores/          # Pinia（auth + plan）
        ├── views/           # 页面（Login/Layout/Chat/Guide/Share/Profile/Logs）
        ├── lib/api.ts       # API 客户端
        └── components/      # 组件（PlannerForm / IntentConfirm / AnalyzeProgress / FinalSummary / GuideDetailModal）
```

## 🚀 快速开始

### 1. 启动依赖服务

```bash
# Redis（会话锁，必需：缺它 /api/chat/* 直接 500）
docker-compose up -d redis                 # → localhost:6379
# Qdrant（向量库，必需）：需监听 6333（容器或本机服务）
# PostgreSQL（攻略真相源，必需）：需与 .env 的 DATABASE_URL / PG_DSN 一致
# Elasticsearch（词法检索）：
#   本机原生 9.5.3 → E:\elasticsearch-9.5.3\bin\elasticsearch.bat
#   容器版需显式指定 profile（否则不会启动）：
#     docker-compose --profile full up -d elasticsearch kibana
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 API Key
```

### 3. 启动后端

```bash
pip install -r requirements.txt
python run_backend.py
# 服务启动于 http://localhost:8088
# 控制台输出会同时写入 logs/backend.log（查看方式见「🖥️ 查看后端日志」一节）
```

> 也可以直接用 `python -m uvicorn main:app --port 8088`，但那样输出只在当前终端里，
> 独立日志服务（log_viewer.py）读的是日志文件，会看不到内容。

### 4. 启动前端

```bash
cd vue
npm install
npm run dev
# 开发服务器: http://localhost:5173
```

### 5. 启动小红书 MCP（可选）

```bash
python start_mcp.py
```

## 📖 功能说明

### 聊天（三种模式）
| 模式 | 检索范围 | 行为 |
|------|---------|------|
| 💬 普通（默认） | 不检索 | 直接由模型回答 |
| 🧠 历史模式 | 只检索**自己**的攻略 | 先做关键词 + LLM 意图判定，判定需要才检索 |
| 🌐 公开模式 | **自己的 + 他人已公开的**攻略 | 每次提问都检索攻略库（引用他人攻略会标注来源） |

### 旅游攻略
- 输入旅行需求 → 意图识别 → 确认 → 后台分析任务（SSE 实时进度）→ 生成攻略
- 进度与状态在 Pinia store 里：切页面不中断，刷新页面自动追平
- 最终总结自动做格式/结构校验后写入知识库（校验不通过则不写，过程可在页面看到）
- 生成后的攻略在「我的」里可查看详情、评价、公开/取消公开

### 攻略搜索
- 「我的攻略」「他人分享」都支持按标题/目的地/摘要/关键词搜索（后端过滤 + 分页）

### 旅游分享
- 在"我的攻略"中评价并公开攻略（**需先评价才能公开**）
- 公开后出现在"他人分享"页面，其他用户可浏览、参考

### 小红书登录（生成攻略的前置条件）
- 攻略页顶部有**状态灯 + 「登录小红书」按钮**：未登录时输入框与「开始规划」禁用
- 点按钮 → **网页弹出二维码**（由 MCP 的 `get_login_qrcode` 生成）→ 用小红书 App 扫码 → 灯变绿
- 服务器**不需要**桌面环境/浏览器窗口，用户也**不需要**上传任何文件
- ⚠️ Linux 服务器请先执行一次 `sudo bash deploy/install-xhs-deps.sh`
  （MCP 会自己下载无头 Chromium，但**不会**装它依赖的系统库；不装会报
  `error while loading shared libraries: libatk-1.0.so.0`。页面会自动识别该故障并显示这条命令）
- 一人一实例（独立工作目录 + 独立端口）：cookies 互不覆盖、账号互不干扰，空闲 30 分钟自动回收
- 换号：点「🔄 换个账号」→ 自动退出登录并出新码；「📄 导入 cookies.json」仅为迁移已有登录态的高级选项
- 详见 **`docs/xhs-multi-user.md`**（含 Linux 服务器步骤、Docker 方案、容量估算与排错）

### 并发控制
- `XHS_MAX_CONCURRENT`：同时使用小红书 MCP 的用户数上限（共享模式）
- `XHS_MAX_INSTANCES`：多用户模式下同时在线实例上限（默认 3），超出会提示稍后再试

## 🧪 测试

```bash
pip install -r requirements-dev.txt
python -m pytest              # 75 个单测，不依赖任何外部服务（ES/LLM/MCP/Redis 全部 mock）
```

覆盖：认证流程、聊天三模式与 SSE 端点、**分析任务归属与越权防护**、
小红书多用户管理（目录隔离/端口分配/查状态不拉实例）、RAG 可见性映射。

`test/manual/` 下是手动/联调脚本（会真的调用 LLM、小红书 MCP、数据库），
`pytest.ini` + `test/conftest.py` 已做双重保护，不会被自动收集。详见 `test/README.md`。

## 🖥️ 查看后端日志（独立服务 + 固定口令）

日志里含请求路径、task_id、用户信息，因此**不挂在主应用上**（`/api/dev/logs` 默认关闭），
而是独立的服务：

```bash
python log_viewer.py                     # 默认 http://127.0.0.1:8099，用户名 admin
LOG_VIEWER_PASSWORD='你的口令' python log_viewer.py
# 服务器上想让内网能访问：
LOG_VIEWER_HOST=0.0.0.0 LOG_VIEWER_PASSWORD='你的口令' python log_viewer.py
```

浏览器打开 → HTTP Basic 输入 `.env` 里的 `LOG_VIEWER_USER / LOG_VIEWER_PASSWORD` → 实时跟随日志
（支持在多个文件间切换、关键字过滤、暂停/清屏）。

也可以直接看文件：`Get-Content logs\backend.log -Wait -Tail 50`，或服务器上 `journalctl -u travel-agent-api -f`。
前端界面**不再**提供日志页（避免任何登录用户都能看全站日志）。

## ☁️ 云端部署

* **`docs/deployment-runbook.md`** —— 可照做的部署流程（Ubuntu 单机 + Nginx + HTTPS + systemd + 备份 + 验收）
* **`docs/deployment-audit.md`** —— 多用户场景的现状评估、P0/P1 清单、结论
* **`deploy/`** —— `nginx.conf` / 两个 systemd 单元 / `backup.sh` / `check.sh`（部署后验收）

关键约束：**必须单副本单 worker**（分析任务表与小红书实例表都在进程内）。

```bash
# 只跑依赖服务（PG/Qdrant/Redis/ES）
docker compose up -d redis qdrant
# 跑后端 + 独立日志服务
docker compose --profile app up -d api log-viewer
```

## 📤 推送到 GitHub（推送前必做两个自查）

```bash
python check_secrets.py     # ① 密钥/凭据：扫描 git 已跟踪文件，命中即非 0 退出
python check_repo.py        # ② 仓库完整性：源码有没有被 .gitignore 误伤
git add -A && git commit -m "..." && git push
```

**为什么要 ②**：`.gitignore` 里**没锚定到仓库根**的目录名规则会误伤嵌套的同名源码目录。
真实踩过的坑：Python 打包产物规则 `lib/` 把 `vue/src/lib/` 整个忽略了，
`vue/src/lib/api.ts` 从未进仓库 —— 本地 `npm run build` 正常，服务器 clone 下来直接
一片 `Cannot find module '@/lib/api'`（TS2307）。所以：

* 目录名类规则一律写成 `/xxx/`（锚定根目录），并写清注释；
* 推送前跑 `python check_repo.py`（它会逐文件比对 `services/ controller/ vue/ …`，
  列出没进仓库的源码并直接告诉你命中的是哪条规则）；
* 服务器上拉完代码可以先确认一下：`git ls-files vue/src/lib`。

## 🔑 环境变量

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | DeepSeek 接口地址 |
| `DEEPSEEK_MODEL` | DeepSeek 主模型 |
| `DEEPSEEK_FREE_MODEL` | DeepSeek 免费模型 |
| `KIMI_API_KEY` | Kimi API Key |
| `KIMI_BASE_URL` | Kimi 接口地址 |
| `KIMI_MODEL` | Kimi 模型 |
| `GAODE_API_KEY` | 高德地图 API Key |
| `JWT_SECRET_KEY` | JWT 签名密钥 |
| `ES_URL` | Elasticsearch 地址 |
| `EMBEDDING_API_KEY` | Embedding API Key |
| `EMBEDDING_BASE_URL` | Embedding API 地址 |
| `EMBEDDING_MODEL` | Embedding 模型名 |
| `EMBEDDING_DIM` | 向量维度 |
| `XHS_MAX_CONCURRENT` | 小红书 MCP 最大并发（共享模式） |
| `XHS_MULTI_USER` | 是否一人一实例（默认 true；false = 共享一个账号） |
| `XHS_MAX_INSTANCES` | 多用户模式同时在线实例上限（默认 3） |
| `CORS_ORIGINS` | 允许跨域的前端来源（逗号分隔，别用 `*`） |
| `APP_ENV` | `development` / `production`（production 会对危险默认值报错） |
| `LOG_VIEWER_PASSWORD` | 独立日志服务的固定口令（**务必修改**） |
| `RAG_CHAT_VISIBILITY` | **历史模式**检索范围：`own`（默认，只检索自己的）/ `own_or_public` / `public` / `all`；公开模式固定"自己的 + 他人公开" |
| `RAG_AUTO_INGEST` | 生成的攻略是否自动写入知识库（默认 true） |
| `BACKEND_LOG_FILE` | 后端日志文件路径（默认 `logs/backend.log`，独立日志服务读它） |

## 📡 API 接口

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `POST` | `/api/auth/register` | 注册 | ❌ |
| `POST` | `/api/auth/login` | 登录 | ❌ |
| `POST` | `/api/chat/message` | 聊天（`chat_mode`: normal/history/public） | ✅ |
| `POST` | `/api/chat/stream` | 聊天流式（SSE，同上） | ✅ |
| `GET` | `/api/chat/history` | 聊天历史 | ✅ |
| `POST` | `/api/guides/save` | 保存攻略 | ✅ |
| `GET` | `/api/guides/my` | 我的攻略（`keyword` 搜索） | ✅ |
| `GET` | `/api/guides/{id}` | 攻略详情（本人或他人已公开） | ✅ |
| `POST` | `/api/share/{id}/rate` | 评价攻略 | ✅ |
| `POST` | `/api/share/{id}/publish` | 公开攻略 | ✅ |
| `GET` | `/api/share/public` | 公开攻略列表（`keyword` 搜索） | ❌ |
| `GET` | `/api/user/profile` | 用户信息 | ✅ |
| `GET` | `/api/user/stats` | 统计数据 | ✅ |
| `POST` | `/api/intent/recognize` | 意图识别 | ✅ |
| `POST` | `/api/summary/stream` | SSE 分析流（订阅进度，`from_index` 可重放） | ✅ |
| `GET` | `/api/summary/task/{task_id}` | 分析任务快照（刷新后追平进度） | ✅ |
| `POST` | `/api/summary/cancel/{task_id}` | 取消分析 | ✅ |
| `GET` | `/api/dev/logs` | 后端日志快照（开发调试） | ✅ |
| `GET` | `/api/dev/logs/stream` | 后端日志实时跟随（SSE） | ✅ |
| `GET` | `/api/mcp/status` | MCP 状态 | ❌ |

**安全清单（务必确认）**

| 项 | 说明 |
|----|------|
| `.env` | 已被忽略；里面是真实 Key，**永远不要提交** |
| `.env.example` | 会提交，只能放占位值（`your_xxx`）。曾经误填过真实 DeepSeek Key，已替换为占位值 —— 如果那把 Key 仍在使用，建议到控制台**吊销重发** |
| `logs/` | 已被忽略（含请求内容与业务日志） |
| `xiaohongshumcp/` | 已被忽略（两个 20MB+ exe + `cookies.json` 登录凭据） |
| `.venv/`、`node_modules/` | 已被忽略（本机 `.venv` 173MB） |
| `.gitignore` 里的 `/_*.py` | 只忽略根目录的临时调试脚本；**不要**写成 `_*.py`，否则会连 `__init__.py` 一起忽略，新克隆会 import 失败（已用 `!**/__init__.py` 兜底） |
