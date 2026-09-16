# 云端部署流程（Ubuntu 单机版，可直接照做）
===============================================================================

目标形态：**一台云服务器 + 一个域名**，前后端同域部署，日志走独立端口 + `.env` 里的固定口令。

```
用户浏览器 ──HTTPS(443)──▶ Nginx ──┬── /            前端静态文件（vue/dist）
                                   └── /api/         反向代理 → 127.0.0.1:8088（FastAPI）
                                                        │
                ┌───────────────────────────────────────┼──────────────────────────┐
                ▼                    ▼                  ▼                          ▼
        PostgreSQL(真相源)      Redis(会话锁)      Qdrant(向量)              Elasticsearch(词法)
                                                        ▲
你的电脑 ──SSH 隧道 8099──▶ 日志服务 log_viewer.py（固定口令，只有你能看）
```

> 架构约束（先读）：`docs/deployment-audit.md`。
> **必须单副本单 worker**：分析任务表与小红书实例表都在进程内，横向扩容会丢任务。

---

## 0. 前置条件

| 项 | 要求 |
|----|------|
| 服务器 | Ubuntu 22.04/24.04，**2 核 4 GB 起**（ES 吃内存；跑得紧就换成托管 ES） |
| 磁盘 | 40 GB SSD 起（PG 数据 + ES 索引 + Docker 镜像） |
| 域名 | 一条 A 记录指向服务器公网 IP（例：`travel.example.com`） |
| 端口 | 安全组只放行 80 / 443；**不要**放行 8088 / 8099 / 5432 / 9200 |
| 密钥 | DeepSeek API Key、Embedding(DashScope) API Key、高德 Key |

---

## 1. 基础环境

```bash
# 以 root 或 sudo 用户执行
apt update && apt upgrade -y
apt install -y python3.12-venv python3-pip nginx git curl ufw

# Docker（跑 PG / Redis / Qdrant / ES）
curl -fsSL https://get.docker.com | sh
usermod -aG docker $USER    # 重新登录生效

# 防火墙：只放 80/443（8099 走 SSH 隧道，不开公网）
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable

# 专用账号（不要用 root 跑服务）
useradd -m -s /bin/bash travelagent
mkdir -p /opt/travelagent && chown travelagent:travelagent /opt/travelagent
```

---

## 2. 依赖服务（PG / Redis / Qdrant / ES）

```bash
sudo -u travelagent git clone https://github.com/<你>/<仓库>.git /opt/travelagent
cd /opt/travelagent

# 只起依赖服务；ES 在 full profile 里
sudo -u travelagent docker compose up -d postgres redis qdrant
sudo -u travelagent docker compose --profile full up -d elasticsearch
sudo -u travelagent docker compose ps        # 确认都 healthy
```

**用托管服务更省心**（推荐生产）：把 PG/Redis/ES 换成云厂商托管，
下面 `.env` 里对应改 `DATABASE_URL` / `REDIS_URL` / `ES_URL` 即可，Qdrant 也可用 Qdrant Cloud。

---

## 3. 配置 `.env`（含日志口令）

```bash
cd /opt/travelagent
sudo -u travelagent cp .env.example .env
sudo -u travelagent python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # 生成 JWT 密钥
sudo -u travelagent nano .env
sudo chmod 600 .env                       # 里面有全部密钥，权限收紧
```

`.env` 关键项（完整清单见文件内注释）：

```ini
APP_ENV=production
CORS_ORIGINS=https://travel.example.com          # 别用 *，前端域名即可

JWT_SECRET_KEY=<上面生成的随机串>                  # 必改！默认值会被启动日志 ERROR 报出
DEEPSEEK_API_KEY=sk-xxxx
EMBEDDING_API_KEY=sk-xxxx                         # 百炼 DashScope
GAODE_API_KEY=xxxx

# 依赖服务（容器内互访用服务名；托管服务换成对应地址）
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/travel_agent
REDIS_URL=redis://localhost:6379/0
QDRANT_URL=http://localhost:6333
ES_URL=http://localhost:9200

# 后端监听：只绑本机，由 Nginx 反代
BACKEND_HOST=127.0.0.1
BACKEND_PORT=8088
BACKEND_LOG_FILE=/opt/travelagent/logs/backend.log
BACKEND_LOG_ENDPOINT=false                        # 主应用不暴露日志接口（安全默认）

# 独立日志服务：登录口令就写在这里
LOG_VIEWER_HOST=127.0.0.1
LOG_VIEWER_PORT=8099
LOG_VIEWER_USER=admin
LOG_VIEWER_PASSWORD=<你自己的口令>                 # 必改！默认值 logviewer 会告警
LOG_VIEWER_MAX_LINES=3000

# 小红书：Linux 云上没有 Windows exe，先关多用户实例（方案见 docs/xhs-multi-user.md）
XHS_MULTI_USER=false
XHS_MCP_URL=http://<跑 MCP 的机器>:18060/mcp
```

> 口令来源：`log_viewer.py` 启动时会**自动加载项目根目录的 `.env`**，
> 所以你只需要改 `.env`，不需要在命令行传环境变量。

---

## 4. Python 依赖 + 首次初始化

```bash
cd /opt/travelagent
sudo -u travelagent python3 -m venv .venv
sudo -u travelagent .venv/bin/pip install -U pip
sudo -u travelagent .venv/bin/pip install -r requirements.txt

# 自检：能 import 起来、路由数量正常（应为 35）
sudo -u travelagent .venv/bin/python -c "import main; print(len(main.app.openapi()['paths']))"

# 首次启动会建 PG 表并创建 ES 索引（幂等，可以反复跑）
sudo -u travelagent .venv/bin/python -c "
import asyncio
from services import memory_store
asyncio.run(memory_store.initialize())
print('PG/Qdrant/Redis 初始化完成')
"
```

---

## 5. systemd 常驻（后端 + 日志服务）

```bash
sudo cp deploy/travel-agent-api.service deploy/travel-agent-logviewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now travel-agent-api travel-agent-logviewer
systemctl status travel-agent-api --no-pager
curl -fsS http://127.0.0.1:8088/api/health        # 应返回 {"status":"ok",...}
```

⚠️ 单元文件里**没有 `--workers`**，这是有意为之（见架构约束）；也不要 `systemctl scale` 或起第二个副本。

---

## 6. 前端构建 + 静态托管

```bash
# 在本地或服务器上构建都行；服务器上需要 Node 20+
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs

cd /opt/travelagent/vue
sudo -u travelagent npm ci
sudo -u travelagent npm run build          # 产物在 vue/dist
```

前端用相对路径 `/api` 调后端，所以**不需要**配 API 地址，由 Nginx 同域反代即可。

---

## 7. Nginx + HTTPS

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/travel-agent
sudo sed -i 's/example.com/travel.example.com/g' /etc/nginx/sites-available/travel-agent
sudo ln -sf /etc/nginx/sites-available/travel-agent /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 证书（自动改写 nginx 配置并加上 443）
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d travel.example.com --redirect
sudo systemctl reload nginx
```

配置里已经处理两个**容易踩的坑**：
* `proxy_buffering off` + 超时 600s —— 否则聊天/进度/日志这些 SSE 会卡住或一次性吐出；
* `try_files ... /index.html` —— 否则刷新 `/guide` 这类前端路由会 404。

---

## 8. 看日志（你随时排查用）

```bash
# 方式 A（推荐）：SSH 隧道，日志服务完全不暴露公网
ssh -L 8099:127.0.0.1:8099 travelagent@<服务器IP>
# 然后本地浏览器打开 http://127.0.0.1:8099
# 用户名/口令 = .env 里的 LOG_VIEWER_USER / LOG_VIEWER_PASSWORD

# 方式 B：直接看文件 / journald
tail -f /opt/travelagent/logs/backend.log
journalctl -u travel-agent-api -f
```

日志服务支持在 `backend.log`、`xhs-mcp-<用户>.log` 等文件间切换、关键字过滤、暂停/清屏。

---

## 9. 备份（PG 真相源 + 配置 + cookies）

```bash
sudo -u travelagent bash deploy/backup.sh                 # 先手动跑一次确认没问题
sudo crontab -u travelagent -e
# 加入：30 3 * * * /opt/travelagent/deploy/backup.sh >> /var/log/travel-backup.log 2>&1
```

ES / Qdrant 是 PG 的派生索引，可从 PG 重建，所以备份只需 PG dump + `.env` + cookies。
恢复步骤写在 `deploy/backup.sh` 末尾。

---

## 10. 部署后验收

```bash
bash deploy/check.sh https://travel.example.com
```

检查项：健康检查、**未带 token 必须 401**（攻略/聊天/分析任务/小红书状态）、
公开接口 200、主应用日志接口 404、未配置的 Origin 不被放行、
SSE 端点可达、日志服务无口令 401 / 带 `.env` 口令 200。

手工再过一遍：

1. 注册一个账号 → 登录 → 普通模式聊天有回复；
2. 攻略页：未登录小红书时**输入框禁用**（应显示登录引导）；
3. 「我的」保存一篇攻略 → 详情页能打开 → 评价 → 公开 → 匿名窗口访问「旅游分享」能看到；
4. 另一个账号登录 → 在「我的」里**看不到**第 3 步的私有攻略，历史模式聊天也检索不到；
5. 刷新页面时攻略进度不丢（切页/刷新会追平）。

---

## 11. 更新与回滚

```bash
# 更新
cd /opt/travelagent
sudo -u travelagent git fetch --all && sudo -u travelagent git checkout v1.2.0
sudo -u travelagent .venv/bin/pip install -r requirements.txt
cd vue && sudo -u travelagent npm ci && sudo -u travelagent npm run build && cd ..
sudo systemctl restart travel-agent-api travel-agent-logviewer
bash deploy/check.sh https://travel.example.com

# 回滚：切回上一个 tag 后重复上面三步（数据库结构变更前先备份！）
sudo -u travelagent git checkout v1.1.0
```

建议：每次上线打 tag；数据库结构变更前先 `bash deploy/backup.sh`。

---

## 12. 常见问题

| 现象 | 原因 / 处理 |
|------|------------|
| 聊天/进度一直不刷新，最后一次性出现 | Nginx 没关缓冲 → 确认 `location /api/` 里有 `proxy_buffering off` |
| 刷新 `/guide` 404 | Nginx 少了 `try_files $uri $uri/ /index.html` |
| 登录后立刻掉线 | `JWT_SECRET_KEY` 被改过或不同实例不一致（多副本）；确认单副本 + `.env` 一致 |
| 分析进度「任务不存在」 | 起了多 worker/多副本 → 恢复单 worker，见 `docs/deployment-audit.md` |
| 攻略生成报「未搜索到有效帖子」 | 小红书 MCP 未就绪：`XHS_MULTI_USER=false` 时需在别的机器跑 MCP 并配 `XHS_MCP_URL`；多用户模式见 `docs/xhs-multi-user.md` |
| 日志 `Permission denied: .../xiaohongshu-mcp-windows-amd64.exe` | 把 **Windows 版**文件放到 Linux 服务器上了（PE 文件无法执行）→ 换 `xiaohongshu-mcp-linux-amd64` 并 `chmod +x`，或用「导入 cookies.json」 |
| 日志服务打不开 | 只监听 127.0.0.1 属正常 → 用 SSH 隧道；或 `.env` 里设 `LOG_VIEWER_HOST=0.0.0.0` 并只放行内网 |
| 内存吃紧 | ES 最占内存：`ES_JAVA_OPTS=-Xms512m -Xmx1g`；或把 ES 换成托管服务 |
| 端口 8088 被公网扫到 | 说明安全组放行了它：关掉，只留 80/443 |

---

## 13. 成本参考（按月，按需选）

| 方案 | 配置 | 大致费用 | 适用 |
|------|------|---------|------|
| 最小可用 | 2C4G 单机 + Docker 全家桶 | ¥60–120 | ≤5 人，内测 |
| 推荐 | 4C8G 单机 + 托管 PG/Redis | ¥300–600 | ≤50 人 |
| 生产 | 应用机 4C8G + 托管 PG/ES/Qdrant + 对象存储备份 | ¥800+ | 正式对外 |
| 外部 API | DeepSeek + DashScope Embedding + 重排 | 按量，几十元/万次问答 | 与规模成正比 |

> 每用户一次「生成攻略」会调用多次 LLM + embedding + 小红书抓取，务必按 `docs/deployment-audit.md` P0-4
> 加限流与每用户配额，否则容易被刷爆账单。
