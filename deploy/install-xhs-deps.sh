#!/usr/bin/env bash
# ================================================================================
# 安装 xiaohongshu-mcp 内置 Chromium 所需的系统库
#
# 症状（装之前会看到）：
#   [launcher] Failed to launch the browser ...
#   .../browser/chrome: error while loading shared libraries: libatk-1.0.so.0:
#   cannot open shared object file: No such file or directory
#
# 原因：MCP 会自己下载 Chromium 二进制，但**不会**安装它依赖的系统库；
#      最小化的云服务器镜像里这些库默认没有。
#
# 用法（Ubuntu 22.04 / 24.04、Debian 12+）：
#     sudo bash deploy/install-xhs-deps.sh
#
# 装完可用下面命令自检（应无 "not found" 输出）：
#     ldd ~/.cache/xiaohongshu-mcp/browser/*/browser/chrome | grep "not found"
# ================================================================================
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行：sudo bash $0" >&2
  exit 1
fi

echo "== 更新 apt 索引 =="
apt-get update -y

# 常用包（不同发行版命名略有差异，逐个装、装不上的跳过并提示）
PKGS=(
  libatk1.0-0 libatk1.0-0t64          # libatk-1.0.so.0（报错常见缺这个）
  libatk-bridge2.0-0 libatk-bridge2.0-0t64
  libatspi2.0-0 libatspi2.0-0t64
  libcups2 libcups2t64
  libdrm2 libgbm1
  libnss3 libnspr4
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
  libxext6 libxi6 libxtst6
  libx11-6 libx11-xcb1 libxcb1
  libpango-1.0-0 libcairo2 libglib2.0-0
  libexpat1 libfontconfig1 libfreetype6 libdbus-1-3
  fonts-liberation fonts-noto-cjk
  libasound2 libasound2t64            # 24.04 是 libasound2t64
)

echo "== 安装浏览器运行时依赖（缺哪个装哪个） =="
missing_pkgs=()
for p in "${PKGS[@]}"; do
  if dpkg -s "$p" >/dev/null 2>&1; then
    continue
  fi
  if apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1; then
    echo "  ✅ $p"
  else
    missing_pkgs+=("$p")   # 该发行版没有这个包名（例如 t64 变体），忽略即可
  fi
done
[ ${#missing_pkgs[@]} -gt 0 ] && echo "  （以下包名在当前系统不存在，已跳过：${missing_pkgs[*]}）"

echo
echo "== 自检：找出 chromium 还缺哪些库 =="
shopt -s nullglob
BROWSERS=(/home/*/.cache/xiaohongshu-mcp/browser/*/browser/chrome
          /root/.cache/xiaohongshu-mcp/browser/*/browser/chrome)
if [ ${#BROWSERS[@]} -eq 0 ]; then
  echo "  还没下载浏览器（首次点「登录小红书」时才会下载）—— 装完上面的库即可，之后再看这个自检。"
  exit 0
fi

fail=0
for b in "${BROWSERS[@]}"; do
  echo "  检查 $b"
  out=$(ldd "$b" 2>/dev/null | grep "not found" || true)
  if [ -z "$out" ]; then
    echo "    ✅ 依赖齐全"
  else
    echo "    ⚠️ 仍缺："
    echo "$out" | sed 's/^/       /'
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo
  echo "仍有缺失：可尝试安装 libasound2t64（24.04）或按上面列出的 .so 名搜索对应包："
  echo "  apt-file search <缺失的 .so 名>    # 需先 apt install apt-file && apt-file update"
fi

echo
echo "完成。回到网页点「登录小红书」→ 应能出二维码了。"
