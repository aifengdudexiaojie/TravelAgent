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

## 二、用户使用流程（**只需扫码，不需要任何文件操作**）

```
「生成旅游攻略」页顶部
┌──────────────────────────────────────────────────────────────────────┐
│ 🗺️ 生成旅游攻略     [● 小红书 未登录…]  [🔐 登录小红书]                │
├──────────────────────────────────────────────────────────────────────┤
│ 输入旅行需求 …（未登录时输入框与「开始规划」禁用，并提示先登录）        │
└──────────────────────────────────────────────────────────────────────┘
                     ↓ 点击「登录小红书」
        ┌───────────────────────────────┐
        │  📱 用小红书 App 扫码登录      │
        │   ┌───────────────┐           │   ← 二维码由 MCP 生成，后端转 Base64
        │   │   ▓▓▒▒ QR ▒▒▓▓│           │      直接显示在网页里（无需桌面、无需弹窗）
        │   └───────────────┘           │
        │  有效期剩余 118 秒（过期自动刷新）│
        │  [🔄 刷新二维码] [📄 导入 cookies]│
        └───────────────────────────────┘
```

1. 用户打开攻略页 → 前端每 5 秒轮询 `GET /api/xhs/status`（**只读，不会拉起实例**）
2. 未登录时：状态灯灰色/红色，**「开始规划」被禁用**并说明原因
3. 点「🔐 登录小红书」→ `GET /api/xhs/qrcode`：
   * 后端为该用户启动**自己的** MCP 实例（独立工作目录 + 独立端口，首次约 5–10 秒）
   * 调 MCP 的 `get_login_qrcode` 工具拿到二维码（`image/png` 的 Base64）
   * 前端弹窗展示 → **用户用小红书 App 扫码**（弹窗里带倒计时，过期自动刷新）
   * 前端每 2.5 秒查一次状态，登录成功后**自动关闭弹窗并变绿**（显示小红书用户名）
4. 绿灯后即可生成攻略；分析走该用户自己的实例（`summary_task` → `mcp_url_for(user_id)`）
5. 换号：点「🔄 换个账号」→ 确认后自动退出登录（删该用户 cookies）并弹出新二维码
6. 省资源：点「停止实例」或等 30 分钟空闲自动回收；下次登录会重新拉起

> 「📄 导入 cookies.json」只是**高级选项**（例如你已经在别处登录过、想直接迁移登录态），
> 普通用户完全不需要用它。

> ⚠️ 官方提醒：**同一个小红书账号不要同时在多个网页端登录**，否则会把这里的登录态顶下线
> （用手机 App 查看账号信息不受影响）。

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
| GET | `/api/xhs/qrcode` | **取登录二维码**（Base64 PNG + `expires_at`）：网页直接展示，用户手机扫码 |
| POST | `/api/xhs/clear` | 退出登录（换号用）：调 MCP `delete_cookies` + 删该用户 cookies + 停实例 |
| POST | `/api/xhs/cookies` | 导入 cookies.json（**高级**：迁移已有登录态），body：`{"cookies": "<文件内容>"}` |
| POST | `/api/xhs/login` | 桌面环境备用：拉起登录程序弹浏览器扫码 |
| POST | `/api/xhs/mcp/start` | 只启动实例（不起登录窗口） |
| POST | `/api/xhs/mcp/stop` | 停止实例，释放内存/端口 |
| POST | `/api/xhs/logout` | 停止实例（不删登录态） |

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
              │ PG / Redis / ES  │   │ 每用户 MCP 进程（按需拉起）        │
              │ Qdrant（托管）    │   │ xiaohongshu-mcp-linux-amd64     │
              └──────────────────┘   │ 自动下载的无头浏览器（~150MB）      │
                                     │ 独立目录存 cookies.json            │
                                     │ 监听 18100+，仅本机访问             │
                                     └──────────────────────────────────┘
```

要点：
1. 用官方 **Linux x64 构建**（`xiaohongshu-mcp-linux-amd64`）—— 代码按平台自动挑选
   （`services/xhs_manager.resolve_exe()`），放对文件名即可，无需改代码。
   ⚠️ 官方只支持 **macOS Apple Silicon / Windows x64 / Linux x64**；Linux ARM64 与 macOS Intel 没有构建。
2. **服务器上不能放 Windows 的 `.exe`**：Linux 上会报 `Permission denied` 或 `Exec format error`；
   现在这种情况会在状态接口与前端直接给出「这是 Windows 可执行文件，当前服务器是 linux」的明确提示。
3. **登录不需要桌面环境**：二维码由 MCP 的 `get_login_qrcode` 工具生成，后端转 Base64 给前端展示，
   用户手机扫码即可 —— 不需要弹浏览器、不需要 Xvfb/VNC、也不需要用户上传 cookies 文件。
4. **浏览器二进制不用手动装，但它依赖的系统库要装**：MCP 首次运行会自己下载无头 Chromium
   （约 150MB，落在 `~/.cache/xiaohongshu-mcp/browser/<版本>/browser/chrome`），
   可是 Chromium 依赖的那一堆系统库（libatk / libnss3 / libgbm …）**不会**被一起装上，
   而最小化的云服务器镜像里恰好没有 —— 表现为实例直接起不来：
   `chrome: error while loading shared libraries: libatk-1.0.so.0: cannot open shared object file`。
   解决办法见下面 **「浏览器运行库（每台服务器做一次）」** —— 一条命令，约 1 分钟。
5. 每用户实例按需创建、空闲（默认 30 分钟）销毁；cookies 存在该用户的工作目录里，不要放进镜像。
6. 容量估算（按每实例 1 个浏览器进程）：

   | 规模 | 常驻实例 | 内存 | 建议 |
   |------|---------|------|------|
   | ≤3 人同时生成攻略 | 1–3 | 1–2 GB | 单机 4 GB 足够 |
   | ~10 人 | 3（上限） | 2–3 GB | 8 GB 机型 + 排队提示 |
   | ~50 人 | 需要队列 | 8 GB+ | 实例池 + 任务队列（生成攻略是分钟级任务，适合排队） |

### 浏览器运行库（每台服务器做一次）

MCP 下载的 Chromium 需要一堆系统库；Ubuntu/Debian 最小镜像里没有。**每台服务器执行一次即可**：

```bash
cd /opt/travelagent && git pull          # 先拿到 deploy/install-xhs-deps.sh
sudo bash deploy/install-xhs-deps.sh
```

脚本会 `apt-get install` 全部依赖（自动跳过该系统不存在的包名，例如 24.04 的 `libasound2t64`），
最后用 `ldd` 自检并打印还缺什么。等价的手工命令：

```bash
sudo apt-get update && sudo apt-get install -y \
  libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libcups2 libdrm2 libgbm1 libnss3 libnspr4 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxext6 libxi6 libxtst6 \
  libx11-6 libx11-xcb1 libxcb1 libpango-1.0-0 libcairo2 libglib2.0-0 libexpat1 \
  libfontconfig1 libfreetype6 libdbus-1-3 fonts-liberation fonts-noto-cjk
# Ubuntu 24.04 上 libasound2 改名为 libasound2t64：
sudo apt-get install -y libasound2t64 || sudo apt-get install -y libasound2
```

自检（**没有输出**才是正常的）：

```bash
ldd ~/.cache/xiaohongshu-mcp/browser/*/browser/chrome | grep "not found"
```

装好后不用重启后端：回到网页点「登录小红书」→ 出二维码 → 手机扫码。

> 前端和接口都会**自动识别这类故障**（后端用 `ldd` 查缺哪些库，最多缓存 60 秒），
> 直接把上面这条命令显示在页面上并提供「复制命令」按钮，所以照抄页面提示也能修。

### Linux 服务器落地步骤

```bash
# ① 删掉误传上来的 Windows 版文件（在 Linux 上不可执行，且占 25MB）
cd /opt/travelagent/xiaohongshumcp
rm -f xiaohongshu-mcp-windows-amd64.exe xiaohongshu-login-windows-amd64.exe

# ② 只需要这一个文件（文件名以 releases 页面为准）
wget https://github.com/xpzouying/xiaohongshu-mcp/releases/latest/download/xiaohongshu-mcp-linux-amd64
chmod +x xiaohongshu-mcp-linux-amd64
ls -l                                   # 确认可执行

# ③ 拉新代码并重启（含扫码登录功能）
cd /opt/travelagent && git pull
sudo systemctl restart travel-agent-api
cd vue && npm ci && npm run build && cd ..

# ④ 验证：让接口拉起实例并返回二维码（不需要浏览器/桌面）
curl -s -H "Authorization: Bearer <你的token>" http://127.0.0.1:8088/api/xhs/qrcode \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['ok'], len(d.get('image_base64','')), d.get('expires_at'))"
```

然后**在网页上点「登录小红书」→ 手机扫码**即可，用户侧不需要任何文件操作。

> 想更省事也可以用官方 Docker 镜像（内置浏览器与中文字体）：
> `docker pull xpzouying/xiaohongshu-mcp`，把 `./data` 挂到该用户的工作目录、端口映射到实例端口。
> 本项目的 `services/xhs_manager.py` 默认按"本地二进制"方式拉起；用容器时把 `XHS_MCP_EXE`
> 指向你自己的启动脚本（脚本内 `docker run`）即可接入。

## 六、登录态迁移（高级）：导入 cookies.json

**普通用户用不到这一节** —— 直接扫码即可。以下场景才需要：

* 你在**另一台机器**上已经登录过小红书，想把那个登录态直接搬到服务器；
* 或者你的账号扫码环境受限（例如需要在特定网络下登录）。

步骤：

1. 在已登录的机器上找到 `cookies.json`（当前项目：`xiaohongshumcp/cookies.json` 或
   `xiaohongshumcp/users/<user_id>/cookies.json`）；
2. 打开 `http://<你的站点>/#/guide` → 点「🔐 登录小红书」→ 弹窗里的「📄 导入 cookies.json」→ 选择该文件；
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
| 日志 `Permission denied: '.../xiaohongshu-mcp-windows-amd64.exe'` | 把 **Windows 版**文件放到 Linux 服务器上了（PE 文件在 Linux 上无法执行） | 按第五节换 Linux 构建 |
| `Exec format error` | 同上（有的内核先报权限再报格式） | 同上 |
| 状态里 `exe_error` 提示"未找到 linux 可执行文件" | 只放了 Windows 版 / 文件名不对 | 确认文件名为 `xiaohongshu-mcp-linux-amd64` 且 `chmod +x` |
| `chrome: error while loading shared libraries: libatk-1.0.so.0 ...` | **服务器缺 Chromium 的系统库**（MCP 只下载浏览器，不装依赖） | 执行一次 `sudo bash deploy/install-xhs-deps.sh`，见第五节「浏览器运行库」；页面上也会直接显示这条命令 |
| 日志 `Failed to launch the browser` + 上面那条缺库报错 | 同一条问题，`libatk-1.0.so.0` 只是第一个缺的库 | 同上（装完再用 `ldd ... \| grep "not found"` 自检） |
| **弹窗一直显示「实例启动中…」** | 首次运行 MCP 要下载无头浏览器（~150MB），启动较慢（正常 1–2 分钟） | 等它出图即可；超过 3 分钟就到 `logs/xhs-mcp-<user_id>.log` 看进度，或用 `XHS_START_TIMEOUT` 调大等待 |
| 弹窗显示红色错误 + 「查看实例日志」 | 取码硬失败（缺可执行文件、平台不对、实例崩溃等） | 点开日志尾部按提示处理；常见的就是上面两条 |
| 二维码扫了没反应 | 二维码已过期（默认 2 分钟左右） | 弹窗会自动刷新，也可点「🔄 刷新二维码」 |
| 之前能搜、某天开始搜不到 | 小红书登录态过期 / 在别处登录把这里顶下线 | 点「🔄 换个账号」重新扫码 |
| 实例起得来但搜索结果为空 | 正常，可能是关键词没结果；也可能是被风控 | 换个关键词试；查日志 |
| 多人同时用超时 | 实例数达上限（`XHS_MAX_INSTANCES`，默认 3） | 调大上限（注意内存）或让用户错峰 |

> 排查入口：`logs/xhs-mcp-<user_id>.log`（实例日志）、`logs/xhs-login-<user_id>.log`（桌面登录程序日志），
> 都可在独立日志服务 `python log_viewer.py` 的页面里切换查看。

## 八、合规与风控提醒

* 抓取小红书内容请遵守平台条款与 robots 约定，控制频率；本项目默认每个关键词只取 5 条、串行分析。
* 用户 cookies 属于个人凭据：只存在其本人目录、不要跨用户复制、不要进版本库（`.gitignore` 已排除
  `xiaohongshumcp/` 与 `cookies.json`）。
* 生成的攻略会写入知识库；默认可见性为"仅本人"，公开需用户显式评价后再点「公开分享」。
