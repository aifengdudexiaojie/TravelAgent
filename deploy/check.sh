#!/usr/bin/env bash
# ================================================================================
# 部署后验收检查（照着 docs/deployment-runbook.md 部署完后跑一遍）
#
# 用法：
#   bash deploy/check.sh https://your-domain.com
#   # 只查本机后端：bash deploy/check.sh http://127.0.0.1:8088
# ================================================================================
set -uo pipefail

BASE="${1:-http://127.0.0.1:8088}"
LOG_VIEWER="${LOG_VIEWER:-http://127.0.0.1:8099}"
pass=0; fail=0

ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ❌ $1"; fail=$((fail+1)); }
check() { # <描述> <期望码> <实际码>
  if [ "$2" = "$3" ]; then ok "$1（HTTP $3）"; else bad "$1（期望 $2，实际 $3）"; fi
}

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" 2>/dev/null; }

echo "== 1. 后端健康 =="
HEALTH=$(curl -s --max-time 10 "$BASE/api/health" || true)
echo "$HEALTH" | grep -q '"status":"ok"' && ok "GET /api/health 返回 ok" || bad "GET /api/health 异常：$HEALTH"
echo "$HEALTH" | grep -q '"status": *"ok"' && true

echo "== 2. 鉴权边界（未带 token 必须 401） =="
check "GET  /api/guides/my"        401 "$(code "$BASE/api/guides/my")"
check "POST /api/chat/stream"      401 "$(code -X POST -H 'Content-Type: application/json' -d '{"message":"hi"}' "$BASE/api/chat/stream")"
check "GET  /api/summary/task/1"   401 "$(code "$BASE/api/summary/task/1")"
check "GET  /api/xhs/status"       401 "$(code "$BASE/api/xhs/status")"

echo "== 3. 公开接口可用 =="
check "GET  /api/share/public"     200 "$(code "$BASE/api/share/public")"

echo "== 4. 主应用不应暴露日志接口（安全默认） =="
check "GET  /api/dev/logs"         404 "$(code "$BASE/api/dev/logs")"

echo "== 5. CORS 只允许配置的域名 =="
CORS=$(curl -s -D - -o /dev/null --max-time 10 -H "Origin: https://evil.example" "$BASE/api/health" \
       | grep -i '^access-control-allow-origin' || true)
if echo "$CORS" | grep -q 'evil.example'; then bad "恶意 Origin 被放行：$CORS"; else ok "未放行未配置的 Origin"; fi

echo "== 6. SSE 能连（前端进度流依赖） =="
SSE=$(curl -s --max-time 5 -X POST -H 'Content-Type: application/json' \
      -d '{"message":"hi","chat_mode":"normal"}' "$BASE/api/chat/stream" \
      -o /dev/null -w '%{http_code}' || true)
if [ "$SSE" = "401" ] || [ "$SSE" = "200" ]; then ok "SSE 端点可达（HTTP $SSE）"; else bad "SSE 端点异常（HTTP $SSE）"; fi

echo "== 7. 独立日志服务（应 401 后才需要口令） =="
check "GET  $LOG_VIEWER/api/logs（无口令）" 401 "$(code "$LOG_VIEWER/api/logs")"
ENV_FILE=""
for candidate in "${APP_DIR:-.}/.env" "/opt/travelagent/.env" "./.env"; do
  [ -f "$candidate" ] && ENV_FILE="$candidate" && break
done
PWD_HINT=""; USER_HINT="admin"
if [ -n "$ENV_FILE" ]; then
  PWD_HINT=$(grep -E '^LOG_VIEWER_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
  USER_HINT=$(grep -E '^LOG_VIEWER_USER=' "$ENV_FILE" | head -1 | cut -d= -f2- || echo admin)
fi
if [ -n "$PWD_HINT" ]; then
  check "GET  $LOG_VIEWER/api/logs（带 $ENV_FILE 里的口令）" 200 "$(code -u "$USER_HINT:$PWD_HINT" "$LOG_VIEWER/api/logs?tail=1")"
else
  echo "  ⚠️  未读到 LOG_VIEWER_PASSWORD（可选：export APP_DIR=/opt/travelagent），跳过后半段检查"
fi

echo
echo "通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ] && echo "✅ 验收全部通过" || echo "❌ 有失败项，请对照 docs/deployment-runbook.md 排查"
exit "$fail"
