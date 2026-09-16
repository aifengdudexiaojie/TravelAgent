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
        ┌───────────────────────────────────────────┐
        │  📱 用小红书 App 扫码登录                  │
        │   ┌───────────────┐                       │
        │   │   ▓▓▒▒ QR ▒▒▓▓│  ← 服务器**实时**读取 │
        │   └───────────────┘    每 2.5 秒自动更新   │
        │  这是第 N 次读取的最新二维码（不会扫到过期码）│
        │  [🔄 重新获取二维码] [退出登录]            │
        └───────────────────────────────────────────┘
```

1. 用户打开攻略页 → 前端每 2.5 秒轮询 `GET /api/xhs/status`（**只读，不会拉起实例**）
2. 未登录时：状态灯灰色/红色，**「开始规划」被禁用**并说明原因
3. 点「🔐 登录小红书」→ `POST /api/xhs/login/start`（**推荐路径，见下节**）：
   * 服务器启动一个浏览器打开小红书登录页，把**当前**二维码返回给前端
   * 前端每 2.5 秒 `GET /api/xhs/login/probe` —— **每次都带回最新那张码**
     （小红书自己的码约 1~2 分钟换一次，所以"每次都是新图"才保证扫到的不是废码）
   * 用户用小红书 App 扫码 → **手机上点「确认登录」**
   * 若小红书弹出**二次设备安全验证**，后端会把那张验证码也返回，页面明确提示
     "请再扫这张"，扫完即完成（这一步上游 MCP 做不到，见 issue #799）
   * 登录成功后后端按 MCP 的 v2 格式写入 `cookies.json`（保留 seed），状态灯立刻变绿
4. 绿灯后即可生成攻略；分析走该用户自己的 MCP 实例（`summary_task` → `mcp_url_for(user_id)`）
5. 换号：点「🔄 换个账号」→ 确认后自动退出登录（删该用户 cookies）并重新出码
6. 省资源：点「停止实例」或等 30 分钟空闲自动回收；登录浏览器在弹窗关闭时即关掉

> ⚠️ 官方提醒：**同一个小红书账号不要同时在多个网页端登录**，否则会把这里的登录态顶下线
> （用手机 App 查看账号信息不受影响）。

### 为什么不用 MCP 自带的 `get_login_qrcode`（我们改成了自建登录）

上游 v2.5.0 的实现有三个硬伤，都会表现成"**扫码后毫无反应**"：

| 上游行为（源码依据） | 后果 |
|----------------------|------|
| `GetLoginQrcode` 只截**一张**静态图（`.login-container .qrcode-img`），返回的"在 X 前扫码"是**会话超时**（`now + 4m`），不是二维码有效期 | 小红书自己的码约 1~2 分钟就换/失效，用户稍慢就扫到废码：手机没反应、服务端也检测不到 |
| 只等 `.main-container .user .link-wrapper .channel`，**不检查**二次验证弹窗 `.r-captcha-modal .qrcode-img`（[issue #799](https://github.com/xpzouying/xiaohongshu-mcp/issues/799) 仍在） | 触发风控时要扫第二张码，MCP 永远等不到登录元素 → cookies 永远存不下来 |
| 重复调用 `get_login_qrcode` 会**新建浏览器并取消旧会话**（`login_session.go` 注释写明） | 之前前端"过期自动换码"等于把用户刚确认的会话顶掉 |
| `CheckLoginStatus` 每次调用都 `newBrowser()`（实测 ~4 秒、几百 MB） | 打开攻略页就每几秒启一个 Chromium，小内存云主机直接被拖垮 |

现在我们的做法（`services/xhs_login_browser.py`）：

* **我们自己的浏览器、我们自己读码**：每次探测都重新读一次 `src`，页面上永远是最新那张码；
  探测只是读一个属性，**不会重置登录会话**，所以刷新二维码是安全的；
* **二次验证码也支持**：检测到 `.r-captcha-modal .qrcode-img` 就把那张码返回给前端；
* **登录判定 + 落盘**：`.channel` 元素出现（或重载一次后出现）即视为成功，
  把 `ctx.cookies()` 按 MCP 的 v2 结构写入 `xiaohongshumcp/users/<user_id>/cookies.json`
  （保留文件里已有的 `seed`，因为 MCP 用它绑定浏览器指纹）；
  MCP 每次新建浏览器都会 `LoadCookies()`，所以**不需要重启实例**就生效；
* **指纹一致**：MCP 在 Linux 上会伪装成 Windows Chrome（`WithFingerprint("")`），
  我们的登录浏览器用同一套 UA/locale/时区，避免同一份 cookie 在不同指纹下被判风控；
* MCP 的静态二维码路径保留为**备用**（没装 playwright 时自动退回）。

### 部署这一步（服务器上执行一次）

```bash
cd /opt/travelagent && git pull
sudo bash deploy/install-xhs-login.sh          # 装 playwright + Xvfb（+ 复用已有 Chromium）
bash deploy/install-xhs-login.sh --check       # 自检
sudo systemctl restart travel-agent-api
```

脚本做四件事：① 复用 MCP 已下载的 Chromium（省一次 150MB 下载）；② 装 `playwright`；
③ 建 `travel-agent-xvfb` 常驻虚拟显示（有头模式更接近官方推荐的"桌面登录"路径）；
④ 给后端单元补一行 `Environment=DISPLAY=:99`。

* 想改回无头模式：`.env` 里加 `XHS_LOGIN_HEADLESS=true` 后重启后端；
* `XHS_LOGIN_MAX_SESSIONS`（默认 2）：同时进行的登录浏览器上限；
* `XHS_LOGIN_IDLE`（默认 900 秒）：登录浏览器闲置多久自动关闭。

### 排错：扫码后没反应

| 现象 | 原因 | 处理 |
|------|------|------|
| 页面一直不变绿，二维码却在变 | 手机上没点「确认登录」 | 扫码后在手机上确认；这是最常见的原因 |
| 出现"小红书要求二次安全验证"提示 | 小红书风控要求再扫一张码（**这是正常的**） | 直接扫页面上那张新码即可 |
| 弹窗里显示"服务器未安装 playwright，已退回 MCP 二维码方案" | 没跑安装脚本 | `sudo bash deploy/install-xhs-login.sh` |
| 提示"无头模式下启动失败" | 没有 DISPLAY 且无头启动失败 | 跑安装脚本（建 Xvfb），或查 `deploy/install-xhs-login.sh --check` |
| MCP 方案下扫码没反应 | 上游只截一张码、也不处理二次验证 | 用推荐路径（实时扫码）；备用方案见第六节 cookies 导入 |

我们这边为此修掉的三个隐患（都会造成"扫码后无反应"）：

* **等扫码期间不再查询 MCP**（最重要）。上游每次工具调用都要新起浏览器，而登录
  会话的存活完全依赖"那个浏览器一直活着并在轮询扫码结果"。所以：发出二维码后
  进入静默期（`XHS_SCAN_WINDOW`，默认 300 秒，覆盖 MCP 自己的 4 分钟等待窗口），
  这期间只做两件**零成本**的事：
  * 盯 `cookies.json`：MCP 扫码成功后会重写这个文件（>500B，占位文件只有 99B），
    文件一变新就说明登录成功 → 状态灯立刻变绿，**不需要问 MCP**；
  * 读 `logs/xhs-mcp-<user_id>.log`：MCP 会打印
    `等待扫码登录，会话 #N，超时 4m0s` / `扫码登录成功，cookies 已保存` /
    `登录会话 #N 结束，未检测到扫码`，据此在页面上给出准确结论
    （"还在等"还是"这次没扫上，请重新取码"）。
* **不再自动换码**：以前二维码过期就自动重新取码，而重复调用 `get_login_qrcode`
  会**新建浏览器、取消旧会话**（issue #799 明确提到）。现在默认返回**缓存**的码，
  只有用户点「🔄 重新获取二维码」才真的重新取码。
* **常态查状态改成"短超时 + 缓存"**：`XHS_STATUS_TIMEOUT`（默认 8 秒）避免 MCP 忙时
  把接口挂住；`XHS_STATUS_CACHE`（默认 30 秒）把"每 2.5 秒一次"的轮询收敛成
  每 30 秒最多一次真实探测 —— 否则光是打开攻略页就会让服务器每 4 秒启一个 Chromium。
  查询失败但本地有 cookies.json 时按"已登录"兜底。

弹窗里的「登录诊断」会显示 MCP 状态与实例日志尾部；扫码后 60 秒还没成功会自动展开
并给出上面这些提示；MCP 判定会话结束时也会直接提示"请重新获取二维码"。
（这些是**备用路径**的保障；推荐路径是上一节的自建实时扫码登录。）

## 三、配置项

| 变量 | 默认 | 说明 |
|------|------|------|
| `XHS_MULTI_USER` | `true` | 一人一实例；设 `false` 退回"共享一个账号"的旧模式 |
| `XHS_PORT_BASE` | `18100` | 多用户实例端口起点 |
| `XHS_MAX_INSTANCES` | `3` | 同时在线实例上限（每个 ≈ 一个 Chromium） |
| `XHS_IDLE_TIMEOUT` | `1800` | 空闲回收秒数（0=不回收） |
| `XHS_START_TIMEOUT` | `180` | 实例启动等待秒数（首次要下载 ~150MB 浏览器，别调太小） |
| `XHS_LOGIN_HEADLESS` | `auto` | 扫码登录浏览器是否有头。`auto`=有 `DISPLAY` 就用有头（Xvfb），否则无头 |
| `XHS_LOGIN_MAX_SESSIONS` | `2` | 同时进行的扫码登录浏览器上限（每个 ≈ 一个 Chromium） |
| `XHS_LOGIN_IDLE` | `900` | 登录浏览器闲置多少秒自动关闭 |
| `XHS_LOGIN_BROWSER` | 空 | 指定登录用浏览器路径（默认复用 MCP 下载的那个） |
| `XHS_STATUS_TIMEOUT` | `8` | 查登录状态的 MCP 超时秒数（前端每 2.5s 轮询，必须短；超时返回"MCP 正忙"） |
| `XHS_STATUS_CACHE` | `30` | 登录状态探测结果的缓存秒数。⚠️ 上游每次探测都会**新起一个 Chromium**（~4s），所以不能每次都真查 |
| `XHS_SCAN_WINDOW` | `300` | 备用路径：发出二维码后的"静默期"秒数，期间完全不查 MCP，只靠 cookies.json 与日志判断 |
| `XHS_QR_CACHE` | `120` | 备用路径：二维码缓存秒数（重复向 MCP 取码会取消登录会话） |
| `XHS_HEADLESS` | `true` | MCP 实例是否无头 |
| `XHS_MCP_URL` | `http://localhost:18060/mcp` | 共享模式下的默认实例地址 |

## 四、接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/xhs/status` | 当前用户状态（只读）：`logged_in` / `mcp_running` / `port` / `workdir` / `platform` / `exe_available` / `exe_error` / `message` |
| POST | `/api/xhs/login/start` | **推荐**：开始/继续扫码登录，返回**当前**二维码（state=qr/verify/logged_in/waiting/unavailable） |
| GET | `/api/xhs/login/probe` | 探测登录进度，每次都带回最新二维码；二次验证时返回那张验证码 |
| POST | `/api/xhs/login/refresh` | 换一张新二维码（安全：登录会话就是我们自己的浏览器） |
| POST | `/api/xhs/login/stop` | 关闭登录浏览器（释放内存） |
| GET | `/api/xhs/login/available` | 网页扫码登录是否可用（没装 playwright 时前端自动退回备用方案） |
| GET | `/api/xhs/qrcode` | 备用：MCP 的静态二维码（Base64 + `expires_at`），`?refresh=1` 强制重新取码 |
| POST | `/api/xhs/clear` | 退出登录（换号用）：调 MCP `delete_cookies` + 删该用户 cookies + 停实例 |
| POST | `/api/xhs/cookies` | 备用：导入 cookies.json，body：`{"cookies": "<文件内容>"}` |
| POST | `/api/xhs/login/desktop` | 桌面环境备用：拉起登录程序弹浏览器扫码 |
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

   > ⚠️ **重要性能事实**：上游 `xiaohongshu-mcp` 的每个工具调用都会**新起一个 Chromium**
   > （`service.go` → `newBrowser()`），一次 `check_login_status` 实测约 4 秒、几百 MB。
   > 所以本项目对状态查询做了缓存与静默期（见第二节末与配置表），**不要**把
   > `XHS_STATUS_CACHE` 调成 0，也不要在外面再写脚本高频轮询 MCP。

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
>
> ⚠️ 用 Docker 跑本项目（不是 MCP）时，application 容器里也要能跑扫码登录浏览器：
> 在镜像里加 `pip install playwright && playwright install --with-deps chromium`，
> 并且容器内要有 `DISPLAY`（或设 `XHS_LOGIN_HEADLESS=true` 走无头）。否则那个容器只能走
> 备用方案；此时可以直接把登录容器和业务容器分开——登录浏览器只要把 `cookies.json`
> 写到同一个挂载卷里就行（MCP 每次新建浏览器都会读它）。

## 六、导入 cookies.json（备用方案）

扫码登录是**推荐路径**（第二节），正常情况下不需要本节；以下情况才用：

* 服务器装不了 playwright 与浏览器（机器太小、装不了依赖），只能用 MCP 的静态二维码方案；
* 或者你想把**另一台机器**上已有的登录态直接搬过来（省一次扫码）。

思路：在你自己有桌面的电脑上登录一次（会弹出真实浏览器窗口，能完成二次验证），
把生成的 `cookies.json` 导到服务器上。由于导入是由浏览器读本地文件再 POST，
所以在网页上点几下就能完成，不需要 scp。

步骤：

1. 在本地电脑上拿到 `cookies.json`：
   * 本项目目录里通常已有：`xiaohongshumcp/cookies.json` 或
     `xiaohongshumcp/users/<user_id>/cookies.json`（就是你本机 MCP 跑起来时用的那份）；
   * 想重新生成：在 `xiaohongshumcp/` 目录下运行 `xiaohongshu-login-windows-amd64.exe`
     （macOS 用 `xiaohongshu-login-darwin-arm64`），浏览器窗口里扫码 + 完成二次验证，它会写出 cookies.json。
2. 打开 `http://<你的站点>/#/guide` → 点「🔐 登录小红书」→ 弹窗里的「📄 导入 cookies.json」→ 选择该文件；
3. 后端把它写进**你自己的**工作目录（`xiaohongshumcp/users/<user_id>/cookies.json`，权限 600），
   如果实例在跑会自动重启使其生效；
4. 状态灯变绿（显示小红书用户名）后即可生成攻略。

> 判断文件是不是"真登录态"：内容里应含 `web_session`、`a1`、`webId` 等键，且大小几 KB。
> 只有几十字节的是 MCP 写的占位文件，导入无效。

> 另一种（进阶）做法：在服务器上用 `xvfb-run -a ./xiaohongshu-login-linux-amd64` 配合 VNC/noVNC
> 远程桌面，在真实浏览器里完成二次验证。功能上可行，但比"本机登录 + 导入 cookies"麻烦得多，
> 一般不必走这条路。

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
| 二维码扫了、手机也确认了，页面就是不变绿（**备用** MCP 方案） | 上游只截一张码、也不处理二次验证；且每次查状态都新起 Chromium | 改用**推荐路径**（实时扫码）；页面上的「登录诊断」会显示 MCP 原话与实例日志 |
| 弹窗提示"服务器未安装 playwright" | 没跑安装脚本 | `sudo bash deploy/install-xhs-login.sh`，然后重启后端 |
| 提示"关闭登录浏览器"后立刻又要点登录 | 会话被回收（`XHS_LOGIN_IDLE`，默认 15 分钟） | 重新点「登录小红书」即可（会重新开一个浏览器） |
| 二维码过期后没自动刷新 | 刻意如此：重复取码会取消当前登录会话（issue #799） | 点「🔄 重新获取二维码」用新码重扫 |
| 状态灯长时间停在"未登录"且页面无变化 | 以前 `check_login_status` 用 120s 超时被 MCP 挂住 | 已修：8 秒超时（`XHS_STATUS_TIMEOUT`）并返回"MCP 正忙"；升级到最新代码即可 |
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
