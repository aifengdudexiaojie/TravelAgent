# 小红书 MCP 服务接入指南

用于在旅行规划 Agent 中搜索小红书相关帖子，获取景点、美食、攻略等信息。

## 安装

### 1. 下载 xiaohongshu-mcp

从 GitHub Releases 下载 Windows 版:

**下载地址:**
https://github.com/xpzouying/xiaohongshu-mcp/releases

选择最新版本，下载 `xiaohongshu-mcp-windows-amd64.zip`

### 2. 解压

将 zip 解压到 `backend/xiaohongshu-mcp/` 目录，得到:

```
backend/xiaohongshu-mcp/
├── xiaohongshu-mcp-windows-amd64.exe    ← MCP 服务主程序
└── xiaohongshu-login-windows-amd64.exe  ← 登录工具
```

### 3. 安装 Python 依赖

```bash
pip install httpx
```

## 首次使用：登录

先启动登录工具扫描二维码（只需一次，后续自动保持登录）：

```bash
cd backend/xiaohongshu-mcp
./xiaohongshu-login-windows-amd64.exe
```

用小红书 App 扫描终端显示的二维码完成登录。

## 启动 MCP 服务

**方式一：手动启动（推荐调试用）**

```bash
cd backend/xiaohongshu-mcp
./xiaohongshu-mcp-windows-amd64.exe
```

服务默认运行在 `http://localhost:18060/mcp`

**方式二：用启动脚本（与后端一起启动）**

```bash
cd backend
python start_mcp.py
```

## 验证服务

```bash
# 检查 MCP 服务是否正常
python -c "from xiaohongshu_mcp_client import XiaohongshuMCPClient; c = XiaohongshuMCPClient(); print('MCP 正常' if c.check_health() else 'MCP 离线')"

# 测试搜索
python -c "from xiaohongshu_mcp_client import search_travel_posts; posts = search_travel_posts('成都'); print(f'找到 {len(posts)} 条帖子')"
```

## 在 Agent 中使用

```python
from xiaohongshu_mcp_client import search_travel_posts, XiaohongshuMCPClient

# 方式 1：一键搜索多个关键词
posts = search_travel_posts("成都")
for p in posts:
    print(f"[{p['likes']}赞] {p['title']}")

# 方式 2：获取详情
client = XiaohongshuMCPClient()
detail = client.get_note_detail(posts[0]["note_id"])
```

## 注意事项

- 登录后请勿在浏览器中登录同一个小红书账号，否则会导致 session 失效
- 手机 App 仍可正常使用
- 首次启动时会自动下载 headless 浏览器（约 150MB）
- 如搜索返回空，请检查登录状态是否有效
