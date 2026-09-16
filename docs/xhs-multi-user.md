# 小红书 MCP 多用户使用方案
===============================================================================

## 一、为什么必须"一人一实例"

`xiaohongshu-mcp` 是一个本地服务：它用真浏览器访问小红书，并把登录态写在自己**工作目录**的
`cookies.json` 里，HTTP 接口默认监听 `:18060`。

两个人共用一个实例会发生：

* 共用同一个小红书账号 → 分不清是谁搜的、别人能看到你的浏览行为、限流算在一起；
* **谁点一次"登录"就会覆盖 `cookies.json`**，把另一个人顶下线；
* 一个人搜索把浏览器占住，另一个人只能排队。

因此本项目在 `services/xhs_manager.py` 里做了**一人一实例**：

```
xiaohongshumcp/
├── xiaohongshu-mcp-windows-amd64.exe      主服务（支持 -port / -headless / -token）
├── xiaohongshu-login-windows-amd64.exe    登录工具（打开浏览器扫码，登录态写当前工作目录）
├── cookies.json                           共享模式的登录态
└── users/
    ├── <user_id_A>/
    │   ├── cookies.json                   A 自己的登录态（互不干扰）
    │   └── instance.json                  {port, pid, started_at}
    └── <user_id_B>/
        └── cookies.json
```

* **工作目录隔离** → cookies 互不覆盖（登录工具没有路径参数，靠 cwd 决定，所以必须独立目录）
* **端口隔离** → 每个实例从 `XHS_PORT_BASE`（默认 18100）往上分配
* 实例空闲 `XHS_IDLE_TIMEOUT`（默认 30 分钟）自动回收，释放内存与端口
* 同时在线实例上限 `XHS_MAX_INSTANCES`（默认 3），超出时状态接口会明确提示

## 二、用户使用流程（前端）

```
「生成旅游攻略」页顶部
┌─────────────────────────────────────────────────────────────────┐
│ 🗺️ 生成旅游攻略      [● 小红书 未登录…]  [🔐 登录小红书]          │
├─────────────────────────────────────────────────────────────────┤
│ 输入旅行需求 …（未登录时输入框禁用，并提示"请先登录小红书"）        │
└─────────────────────────────────────────────────────────────────┘
```

1. 用户打开攻略页 → 前端每 5 秒轮询 `GET /api/xhs/status`（**只读，不会拉起实例**）
2. 未登录时：
   * 状态灯灰色/红色，文案「未登录：请扫码登录自己的小红书」
   * **「开始规划」被禁用**，输入框下方红字说明原因
3. 点「🔐 登录小红书」→ `POST /api/xhs/login`：
   * 后端先为该用户启动**自己的** MCP 实例（独立目录 + 端口）
   * 再拉起 `xiaohongshu-login-windows-amd64.exe`（工作目录 = 该用户目录）
   * 用户在弹出的浏览器窗口里扫码；登录成功后 cookies 落到**他自己的**目录，窗口关闭
   * 前端在接下来 ~2 分钟内改为 3 秒轮询一次，状态自动变绿（显示 `已登录：<用户名>`）
4. 绿灯后即可正常生成攻略；分析过程会走该用户自己的实例（`summary_task` → `mcp_url_for(user_id)`）
5. 想重登/换号：点「🔄 重新登录」（会覆盖自己目录里的 cookies，不影响别人）
6. 想省资源：点「停止实例」（或等 30 分钟自动回收）；下次登录会重新拉起

## 三、配置项

| 变量 | 默认 | 说明 |
|------|------|------|
| `XHS_MULTI_USER` | `true` | 一人一实例；设 `false` 退回"共享一个账号"的旧模式 |
| `XHS_PORT_BASE` | `18100` | 多用户实例端口起点 |
| `XHS_MAX_INSTANCES` | `3` | 同时在线实例上限（每个 ≈ 一个 Chromium） |
| `XHS_IDLE_TIMEOUT` | `1800` | 空闲回收秒数（0=不回收） |
| `XHS_START_TIMEOUT` | `45` | 实例启动等待秒数 |
| `XHS_HEADLESS` | `true` | 实例是否无头；调试想看到浏览器时设 `false` |
| `XHS_MCP_URL` | `http://localhost:18060/mcp` | 共享模式下的默认实例地址 |

## 四、接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/xhs/status` | 当前用户状态（只读）：`logged_in` / `mcp_running` / `port` / `workdir` / `platform` / `exe_available` / `exe_error` / `message` |
| POST | `/api/xhs/login` | 启动该用户实例 + 拉起登录程序（**桌面环境**弹窗扫码） |
| POST | `/api/xhs/cookies` | 导入 cookies.json（**无桌面服务器**的登录方式），body：`{"cookies": "<文件内容>"}` |
| POST | `/api/xhs/mcp/start` | 只启动实例（不起登录窗口） |
| POST | `/api/xhs/mcp/stop` | 停止实例，释放内存/端口 |
| POST | `/api/xhs/logout` | 停止实例（cookies 保留在本人目录，重登会覆盖） |

每个用户的 MCP 日志单独成文件：`logs/xhs-mcp-<user_id>.log`、`logs/xhs-login-<user_id>.log`，
可在独立日志服务（`python log_viewer.py`）里切换查看。

## 五、云端部署怎么落地

Windows exe + 弹窗扫码的方案**只适用于本机/内网 Windows 服务器**（用户与服务器在同一台机器时
能看到弹窗）。放到云上要改成下面的形态：

### 方案 A：Linux 服务器 + 每用户容器（推荐）

```
                      ┌───────────────────────────────┐
  浏览器 ──HTTPS──▶   │ Nginx  (TLS + SSE 关缓冲)      │
                      └──────────────┬────────────────┘
                                     │
                      ┌──────────────▼────────────────┐
                      │ 应用容器 (FastAPI, 单副本)     │
                      └──────┬───────────────┬────────┘
                             │               │
              ┌──────────────▼───┐   ┌───────▼─────────────────────────┐
              │ PG / Redis / ES  │   │ 每用户 MCP 容器（按需拉起）        │
              │ Qdrant（托管）    │   │ xiaohongshu-mcp (Linux 构建)     │
              └──────────────────┘   │ + Xvfb 虚拟显示                   │
                                     │ + 独立卷存 cookies.json           │
                                     │ 监听 18060，映射到应用可访问网段    │
                                     └──────────────────────────────────┘
```

要点：
1. 用官方 **Linux 构建**（`xiaohongshu-mcp-linux-amd64`）替换 Windows exe —— 代码已按平台自动挑选
   （`services/xhs_manager.resolve_exe()`），放对文件名即可，无需改代码。
2. **服务器上不能放 Windows 的 `.exe`**：Linux 上会报 `Permission denied` 或 `Exec format error`。
   现在这种情况会在状态接口与前端直接给出「这是 Windows 可执行文件，当前服务器是 linux」的明确提示。
3. 登录：服务器没有桌面，扫码窗口弹不出来 → 用 **「导入 cookies.json」**（第六节），
   或在服务器上装 Xvfb + VNC 后跑 Linux 版登录工具（不推荐，麻烦）。
4. 每用户实例按需创建、空闲销毁；cookies 存在该用户的工作目录里，不要放进镜像。
5. 容量估算（按每实例 1 个 Chromium）：

   | 规模 | 常驻实例 | 内存 | 建议 |
   |------|---------|------|------|
   | ≤3 人同时生成攻略 | 1–3 | 1–2 GB | 单机 4 GB 足够 |
   | ~10 人 | 3（上限） | 2–3 GB | 8 GB 机型 + 排队提示 |
   | ~50 人 | 需要队列 | 8 GB+ | 实例池 + 任务队列（生成攻略是分钟级任务，适合排队） |

### Linux 服务器落地步骤（照着做）

```bash
# ① 删掉误传上来的 Windows 版文件（在 Linux 上不可执行，且占 25MB）
cd /opt/travelagent/xiaohongshumcp
rm -f xiaohongshu-mcp-windows-amd64.exe xiaohongshu-login-windows-amd64.exe

# ② 下载 Linux 版（文件名形如 xiaohongshu-mcp-linux-amd64，以 releases 页面为准）
wget https://github.com/xpzouying/xiaohongshu-mcp/releases/latest/download/xiaohongshu-mcp-linux-amd64
chmod +x xiaohongshu-mcp-linux-amd64        # 代码也会自动补，但建议手动确认
ls -l xiaohongshumcp/                        # 确认有可执行文件

# ③ 浏览器依赖：rod 需要 Chrome/Chromium
sudo apt-get install -y chromium-browser || {
  wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt install -y ./google-chrome-stable_current_amd64.deb
}

# ④ 重启后端（会按平台重新解析可执行文件）
sudo systemctl restart travel-agent-api

# ⑤ 验证：状态里应能看到实例被拉起
curl -s -H "Authorization: Bearer <你的token>" http://127.0.0.1:8088/api/xhs/status | python3 -m json.tool
```

然后在浏览器里点「登录小红书」→ 服务器会拉起 Linux 版实例；**登录态用下面的 cookies 导入完成**。

## 六、无桌面服务器的登录方式：导入 cookies.json

云服务器弹不出扫码窗口，所以提供了「导入 cookies.json」：

1. 在**有桌面的机器**上登录一次小红书（Windows 上就是点「登录小红书」扫码，
   成功后 `xiaohongshumcp/cookies.json` 或 `xiaohongshumcp/users/<user_id>/cookies.json` 会有登录态）；
2. 打开 `http://<你的站点>/#/guide` → 点「📄 导入 cookies.json」→ 选中该文件；
3. 后端把它写进**你自己的**工作目录（`xiaohongshumcp/users/<user_id>/cookies.json`，权限 600），
   如果实例在跑会自动重启使其生效；
4. 状态灯变绿（显示小红书用户名）后即可生成攻略。

命令行等价操作（把文件内容 POST 上去）：

```bash
curl -s -X POST http://127.0.0.1:8088/api/xhs/cookies \
  -H "Authorization: Bearer <你的token>" -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"cookies":open("cookies.json",encoding="utf-8").read()}))')"
```

校验：非空、≤2MB、合法 JSON（对象或数组），否则会返回明确原因。cookies 属于个人凭据：
只存在该用户自己的目录、不要跨用户复制、不要提交到仓库。

### 方案 B：不部署 MCP，改成"自带数据源"


如果不想在云上跑浏览器：
* 只在**本地**用 MCP 抓取，抓到的笔记入库（PG/ES）后，云端只做 RAG 与分析（本项目的 RAG 已就绪）；
* 或者改用官方/第三方 API（需自行评估合规与配额）。

## 七、排错速查

| 现象 | 原因 | 处理 |
|------|------|------|
| 日志 `Permission denied: '.../xiaohongshu-mcp-windows-amd64.exe'` | 把 **Windows 版**文件放到 Linux 服务器上了（PE 文件在 Linux 上无法执行） | 按第五节换 Linux 构建；或用「导入 cookies.json」 |
| `Exec format error` | 同上（有的内核先报权限再报格式） | 同上 |
| 状态里 `exe_error` 提示"未找到 linux 可执行文件" | 只放了 Windows 版 / 文件名不对 | 确认文件名为 `xiaohongshu-mcp-linux-amd64` 且 `chmod +x` |
| 实例起得来但搜索结果为空 | 未登录 / 浏览器依赖缺失 | 导入 cookies.json；`apt install chromium` |
| 状态一直"未登录"但已导入 cookies | 实例没跑起来，或 cookies 过期 | 点「停止实例」再点「登录小红书」重启；重新导入最新 cookies |
| 多人同时用超时 | 实例数达上限（`XHS_MAX_INSTANCES`，默认 3） | 调大上限（注意内存）或让用户错峰 |

## 八、合规与风控提醒

* 抓取小红书内容请遵守平台条款与 robots 约定，控制频率；本项目默认每个关键词只取 5 条、串行分析。
* 用户 cookies 属于个人凭据：只存在其本人目录、不要跨用户复制、不要进版本库（`.gitignore` 已排除
  `xiaohongshumcp/` 与 `cookies.json`）。
* 生成的攻略会写入知识库；默认可见性为"仅本人"，公开需用户显式评价后再点「公开分享」。
