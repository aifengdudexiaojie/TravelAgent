#!/usr/bin/env bash
# ================================================================================
# 把「服务器上的登录浏览器」直接投屏到你的浏览器窗口里（noVNC）
#
# 为什么需要它：
#   小红书登录不只是"扫个码"——扫码后还可能要求**短信验证码**、滑块、
#   点确认框等风控步骤。这些只能由人看着真实浏览器操作，而我们没法猜全页面结构。
#   所以最稳的做法：把服务器上那个浏览器（跑在 Xvfb 虚拟显示上）用 noVNC 投屏，
#   你在网页里直接点、直接输入，登录成功后系统会自动识别并保存登录态。
#
# 装什么：
#   · x11vnc    : 把 Xvfb 的显示 :99 共享出来
#   · novnc+websockify : 把 VNC 变成网页（默认只监听 127.0.0.1:6080，不对外）
#   · openbox   : 一个极小的窗口管理器（让浏览器窗口正常显示/获得焦点）
#
# 用法：
#   cd /opt/travelagent && sudo bash deploy/install-xhs-vnc.sh
#   bash deploy/install-xhs-vnc.sh --check        # 自检
#
# 装完后有两种访问方式（脚本会都打印出来）：
#   方式 A（零配置，推荐先试）：在你电脑上开 SSH 隧道，再访问 http://localhost:6080/vnc.html
#       ssh -L 6080:127.0.0.1:6080 travelagent@<服务器IP>
#   方式 B（好看的网址）：按脚本提示把 deploy/nginx-vnc.conf 引入 nginx 的 server 块，
#       然后访问 http://<你的站点>/vnc/vnc.html?autoconnect=1&resize=scale&path=websockify
# ================================================================================
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/travelagent}"
DISPLAY_NUM="${XHS_DISPLAY:-:99}"
VNC_PORT="${XHS_VNC_PORT:-5900}"
WEB_PORT="${XHS_VNC_WEB_PORT:-6080}"
SCREEN="${XHS_VNC_SCREEN:-1440x900x24}"

as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

if [ "${1:-}" = "--check" ]; then
  echo "== 自检 =="
  systemctl is-active --quiet travel-agent-xvfb && echo "  ✅ Xvfb（$DISPLAY_NUM）" || echo "  ❌ Xvfb 未运行：sudo systemctl start travel-agent-xvfb"
  systemctl is-active --quiet travel-agent-x11vnc && echo "  ✅ x11vnc（127.0.0.1:$VNC_PORT）" || echo "  ❌ x11vnc 未运行"
  systemctl is-active --quiet travel-agent-novnc && echo "  ✅ websockify/noVNC（127.0.0.1:$WEB_PORT）" || echo "  ❌ noVNC 未运行"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$WEB_PORT/vnc.html" || true)
  [ "$code" = "200" ] && echo "  ✅ noVNC 网页可访问（HTTP $code）" || echo "  ❌ noVNC 网页返回 $code"
  (command -v ss >/dev/null && ss -ltn | grep -q ":$VNC_PORT") && echo "  ✅ 端口 $VNC_PORT 在听" || true
  echo
  echo "  在你自己电脑上执行： ssh -L $WEB_PORT:127.0.0.1:$WEB_PORT travelagent@<服务器IP>"
  echo "  然后浏览器打开：     http://localhost:$WEB_PORT/vnc.html?autoconnect=1&resize=scale&path=websockify"
  exit 0
fi

echo "== 0. 前置检查 =="
if [ ! -d "$APP_DIR" ]; then
  echo "❌ 找不到 $APP_DIR（可用 APP_DIR=/你的路径 指定）" >&2; exit 1
fi
echo "  ✅ $APP_DIR"

echo "== 1. 安装 x11vnc / noVNC / websockify / openbox =="
as_root apt-get update -y >/dev/null 2>&1 || true
as_root apt-get install -y --no-install-recommends x11vnc novnc websockify openbox x11-utils >/dev/null 2>&1
for pkg in x11vnc novnc websockify; do
  dpkg -s "$pkg" >/dev/null 2>&1 && echo "  ✅ $pkg" || echo "  ❌ $pkg 安装失败（请检查 apt 源）"
done
NOVNC_DIR=/usr/share/novnc
[ -d "$NOVNC_DIR" ] || { echo "❌ 找不到 $NOVNC_DIR（novnc 包没装好）"; exit 1; }

echo "== 2. Xvfb + 窗口管理器（带 openbox，窗口才能正常显示/聚焦） =="
as_root tee /usr/local/bin/travel-agent-xvfb-start >/dev/null <<EOF
#!/usr/bin/env bash
# 启动虚拟显示，并在上面跑一个极小的窗口管理器
Xvfb $DISPLAY_NUM -screen 0 $SCREEN -nolisten tcp &
XVFB_PID=\$!
sleep 1
# openbox 让新开的浏览器窗口正常映射与获得焦点（noVNC 里才能点/输入）
openbox --sm-disable >/dev/null 2>&1 &
trap 'kill \$XVFB_PID 2>/dev/null' TERM INT
wait \$XVFB_PID
EOF
as_root chmod +x /usr/local/bin/travel-agent-xvfb-start
as_root tee /etc/systemd/system/travel-agent-xvfb.service >/dev/null <<EOF
[Unit]
Description=Xvfb virtual display (+openbox) for Xiaohongshu web login
After=network.target

[Service]
Type=simple
User=travelagent
Group=travelagent
ExecStart=/usr/local/bin/travel-agent-xvfb-start
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "== 3. x11vnc（只监听本机） =="
as_root tee /etc/systemd/system/travel-agent-x11vnc.service >/dev/null <<EOF
[Unit]
Description=x11vnc for the Xiaohongshu login display ($DISPLAY_NUM)
After=travel-agent-xvfb.service
Requires=travel-agent-xvfb.service

[Service]
Type=simple
User=travelagent
Group=travelagent
ExecStart=/usr/bin/x11vnc -display $DISPLAY_NUM -rfbport $VNC_PORT -localhost -nopw -forever -shared -repeat -noxdamage -quiet
Restart=always
RestartSec=3
# -localhost -nopw：只允许本机连接、不设 VNC 密码。对外访问请走 SSH 隧道或 nginx 基本认证，
# 千万不要把这个端口直接暴露到公网。

[Install]
WantedBy=multi-user.target
EOF

echo "== 4. noVNC（websockify，只监听本机） =="
as_root tee /etc/systemd/system/travel-agent-novnc.service >/dev/null <<EOF
[Unit]
Description=noVNC (websockify) for the Xiaohongshu login display
After=travel-agent-x11vnc.service
Requires=travel-agent-x11vnc.service

[Service]
Type=simple
User=travelagent
Group=travelagent
ExecStart=/usr/bin/websockify --web $NOVNC_DIR 127.0.0.1:$WEB_PORT 127.0.0.1:$VNC_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

as_root systemctl daemon-reload
as_root systemctl enable --now travel-agent-xvfb travel-agent-x11vnc travel-agent-novnc >/dev/null 2>&1
sleep 3

echo "== 5. 让后端使用这个显示器（投屏里才能看到浏览器窗口） =="
UNIT=/etc/systemd/system/travel-agent-api.service
if [ -f "$UNIT" ] && ! grep -q '^Environment=DISPLAY=' "$UNIT"; then
  as_root python3 - "$UNIT" "$DISPLAY_NUM" <<'PY'
import sys, pathlib
path, disp = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8")
if "Environment=DISPLAY=" not in text:
    text = text.replace("[Service]\n", f"[Service]\nEnvironment=DISPLAY={disp}\n", 1)
    path.write_text(text, encoding="utf-8")
    print("  已写入 Environment=DISPLAY=" + disp)
PY
  as_root systemctl daemon-reload
  echo "  · 需要重启后端生效：sudo systemctl restart travel-agent-api"
  echo "    ⚠️ 登录浏览器必须跑在这个显示器上（有头模式），否则投屏里看不到它"
else
  echo "  · 无需修改（已配置或单元文件不存在）"
fi

echo "== 6. 自检 =="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$WEB_PORT/vnc.html" || true)
if [ "$code" = "200" ]; then
  echo "  ✅ noVNC 网页正常（HTTP $code）"
else
  echo "  ⚠️  noVNC 网页返回 $code —— 看日志：journalctl -u travel-agent-novnc -n 30 --no-pager"
fi
systemctl is-active --quiet travel-agent-x11vnc && echo "  ✅ x11vnc 在跑" || echo "  ⚠️  x11vnc 没起来"
systemctl is-active --quiet travel-agent-xvfb && echo "  ✅ Xvfb 在跑" || echo "  ⚠️  Xvfb 没起来"

echo
echo "== 装好了，两种打开方式 =="
echo
echo "方式 A（零配置，先试这个）：在你自己电脑上执行 SSH 隧道"
echo "    ssh -L $WEB_PORT:127.0.0.1:$WEB_PORT travelagent@<服务器IP>"
echo "  然后浏览器打开："
echo "    http://localhost:$WEB_PORT/vnc.html?autoconnect=1&resize=scale&path=websockify"
echo
echo "方式 B（直接用站点域名访问）：把下面这行加进你 nginx 站点配置的 server { } 里"
echo "    include /etc/nginx/snippets/xhs-vnc.conf;"
echo "  然后（脚本已把片段放好）："
echo "    sudo cp $APP_DIR/deploy/nginx-vnc.conf /etc/nginx/snippets/xhs-vnc.conf   # 如尚未复制"
echo "    sudo nginx -t && sudo systemctl reload nginx"
echo "  之后访问："
echo "    http://<你的站点>/vnc/vnc.html?autoconnect=1&resize=scale&path=websockify"
echo
echo "打开后在网页里点「连接」，就能看到服务器上的 Chrome："
echo "  · 用小红书 App 扫码；如需短信验证码，直接在这个页面里输入即可"
echo "  · 登录成功后本系统会自动识别并保存登录态（页面上的状态灯会变绿）"
