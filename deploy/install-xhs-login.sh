#!/usr/bin/env bash
# ================================================================================
# 安装「网页扫码登录」所需环境（服务器上一次即可）
#
# 这一步装什么、为什么：
#   1. 浏览器运行库：复用 MCP 已下载的 Chromium（deploy/install-xhs-deps.sh 已覆盖）
#   2. playwright（Python 包）：登录浏览器由我们自己的后端驱动，才能做到
#      "每次探测都返回最新二维码 + 能把二次验证码也显示出来"
#   3. Xvfb（虚拟显示器）：有头模式跑浏览器更接近"桌面登录"这一官方推荐路径，
#      绕开无头浏览器更容易被风控识别的问题（不装也能跑，就退回无头模式）
#
# 用法：
#   cd /opt/travelagent && sudo bash deploy/install-xhs-login.sh
#
# 装完自检：
#   bash deploy/install-xhs-login.sh --check
# ================================================================================
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/travelagent}"
VENV_PY="$APP_DIR/.venv/bin/python"
DISPLAY_NUM="${XHS_DISPLAY:-:99}"
DISPLAY_FILE="/tmp/.X${DISPLAY_NUM#:}-lock"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

if [ "${1:-}" = "--check" ]; then
  echo "== 自检 =="
  [ -x "$VENV_PY" ] && echo "  ✅ venv: $VENV_PY" || echo "  ❌ 没找到 $VENV_PY"
  "$VENV_PY" -c "import playwright; print('  ✅ playwright', playwright.__file__)" 2>/dev/null \
    || echo "  ❌ 未安装 playwright（跑一次本脚本即可）"
  BROWSER=$(ls /home/*/.cache/xiaohongshu-mcp/browser/*/browser/chrome 2>/dev/null | head -1)
  [ -n "$BROWSER" ] && echo "  ✅ 可复用的 Chromium: $BROWSER" \
                    || echo "  ⚠️  还没下载 Chromium（点一次「登录小红书」后会有；Playwright 也会自己带一个）"
  if systemctl is-active --quiet travel-agent-xvfb 2>/dev/null; then
    echo "  ✅ Xvfb 服务在跑（$DISPLAY_NUM，有头模式可用）"
  else
    echo "  ⚠️  Xvfb 服务未运行 → 将使用无头模式（可用：sudo systemctl start travel-agent-xvfb）"
  fi
  exit 0
fi

echo "== 0. 检查项目目录 =="
if [ ! -x "$VENV_PY" ]; then
  echo "❌ 找不到 $VENV_PY —— 请先按 docs/deployment-runbook.md 部署好项目（含 .venv）" >&2
  exit 1
fi
echo "  ✅ $VENV_PY"

echo "== 1. 浏览器运行库（沿用 install-xhs-deps.sh，已装的会跳过） =="
if [ -f "$APP_DIR/deploy/install-xhs-deps.sh" ]; then
  as_root bash "$APP_DIR/deploy/install-xhs-deps.sh" || true
else
  echo "  ⚠️  没找到 deploy/install-xhs-deps.sh，跳过（若浏览器起不来请手动装依赖）"
fi

echo "== 2. 安装 Xvfb（虚拟显示器，有头模式用） =="
as_root apt-get update -y >/dev/null 2>&1 || true
as_root apt-get install -y --no-install-recommends xvfb x11-utils >/dev/null 2>&1 \
  && echo "  ✅ xvfb" || echo "  ⚠️  xvfb 安装失败（不影响无头模式）"

echo "== 3. 安装 playwright（Python 包，装在项目 venv 里） =="
"$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
if "$VENV_PY" -c "import playwright" 2>/dev/null; then
  echo "  ✅ 已安装"
else
  "$VENV_PY" -m pip install playwright && echo "  ✅ playwright" || {
    echo "  ❌ playwright 安装失败：请检查网络/pip 源" >&2; exit 1; }
fi

# 浏览器：优先复用 MCP 下载的 Chromium；没有就让 Playwright 自己装一个
if ls /home/*/.cache/xiaohongshu-mcp/browser/*/browser/chrome >/dev/null 2>&1; then
  echo "  ✅ 复用 MCP 已下载的 Chromium（无需再下载约 150MB）"
else
  echo "  · 没找到 MCP 的 Chromium，安装 Playwright 自带浏览器…"
  "$VENV_PY" -m playwright install chromium && echo "  ✅ playwright chromium" \
    || echo "  ⚠️  浏览器下载失败：可先点一次「登录小红书」让 MCP 下载浏览器，再跑本脚本"
fi

echo "== 4. 配置 Xvfb 常驻服务（给后端提供 DISPLAY） =="
as_root tee /etc/systemd/system/travel-agent-xvfb.service >/dev/null <<EOF
[Unit]
Description=Xvfb virtual display for Xiaohongshu web login
After=network.target

[Service]
Type=simple
User=travelagent
Group=travelagent
ExecStart=/usr/bin/Xvfb $DISPLAY_NUM -screen 0 1280x900x24 -nolisten tcp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
as_root systemctl daemon-reload
as_root systemctl enable --now travel-agent-xvfb >/dev/null 2>&1 \
  && echo "  ✅ travel-agent-xvfb 已启动（$DISPLAY_NUM）" \
  || echo "  ⚠️  Xvfb 服务启动失败（可继续用无头模式）"
rm -f "$DISPLAY_FILE" 2>/dev/null || true

echo "== 5. 让后端用上这个显示器 =="
UNIT=/etc/systemd/system/travel-agent-api.service
if [ -f "$UNIT" ] && ! grep -q '^Environment=DISPLAY=' "$UNIT"; then
  # 在 [Service] 段里补一行 DISPLAY（幂等：已存在就跳过）
  as_root python3 - "$UNIT" "$DISPLAY_NUM" <<'PY'
import sys, pathlib
path, disp = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8")
if "Environment=DISPLAY=" in text:
    sys.exit(0)
text = text.replace("[Service]\n", f"[Service]\nEnvironment=DISPLAY={disp}\n", 1)
path.write_text(text, encoding="utf-8")
print("  已写入 Environment=DISPLAY=" + disp)
PY
  as_root systemctl daemon-reload
  echo "  · 重启后端生效：sudo systemctl restart travel-agent-api"
else
  echo "  · 无需修改（已配置或单元文件不存在）"
fi

echo
echo "== 完成 =="
echo "现在回到网页点「🔐 登录小红书」→ 会显示服务器**实时**读取的二维码。"
echo "若小红书弹出「安全验证 / 请再次扫码」，页面会把那张码也显示出来，再扫一次即可。"
echo
echo "自检：bash $0 --check"
echo "关掉有头模式（改用无头）：在 .env 里加 XHS_LOGIN_HEADLESS=true 后重启后端"
