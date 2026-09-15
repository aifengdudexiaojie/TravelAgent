#!/usr/bin/env bash
# ================================================================================
# 备份脚本（每天跑一次即可，cron 示例见文件末尾）
#
# 设计前提：**PostgreSQL 是攻略真相源**，ES / Qdrant 都是它的派生索引，可以重建。
#   所以备份只需要：PG 全量 dump + .env（含各类密钥与口令）+ 小红书 cookies。
#   重建派生索引：python -c "from services.rag.ingest import IngestOptions, ingest_guides;
#                             from services.rag.store import GuideStores; ..."
#   或直接跑 seed/重建脚本（见 docs/rag-code-map.md 第四章）。
#
# 用法：
#   sudo -u travelagent bash deploy/backup.sh
#   # crontab -e （travelagent 用户）
#   # 30 3 * * * /opt/travelagent/deploy/backup.sh >> /var/log/travel-backup.log 2>&1
# ================================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/travelagent}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/travelagent}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

# ---------- 1) PostgreSQL 全量备份 ----------
# 连接串从 .env 里取（DATABASE_URL），去掉 SQLAlchemy/asyncpg 前缀差异
DB_URL="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"')"
DB_URL="${DB_URL:-postgresql://postgres:postgres@localhost:5432/travel_agent}"
echo "[$(date '+%F %T')] 备份 PostgreSQL → $BACKUP_DIR/pg-$STAMP.dump"
pg_dump --format=custom --no-owner --dbname="$DB_URL" --file="$BACKUP_DIR/pg-$STAMP.dump"

# ---------- 2) 配置与凭据（.env 里有 JWT 密钥、日志口令、各类 API Key） ----------
echo "[$(date '+%F %T')] 备份 .env"
install -m 600 "$APP_DIR/.env" "$BACKUP_DIR/env-$STAMP.bak"

# ---------- 3) 小红书登录态（多用户模式下每人一份；共享模式只有一份） ----------
if [ -d "$APP_DIR/xiaohongshumcp" ]; then
  echo "[$(date '+%F %T')] 备份小红书 cookies"
  tar -czf "$BACKUP_DIR/xhs-$STAMP.tar.gz" \
      -C "$APP_DIR" $(cd "$APP_DIR" && find xiaohongshumcp -maxdepth 3 -name 'cookies.json' 2>/dev/null || true) \
      2>/dev/null || echo "  （没有 cookies.json，跳过）"
fi

# ---------- 4) 可选：Qdrant 快照 ----------
# Qdrant 也能从 PG 重建，但快照更快恢复。启用方式：QDRANT_URL 可访问时执行
if [ "${BACKUP_QDRANT:-0}" = "1" ]; then
  echo "[$(date '+%F %T')] 触发 Qdrant 快照"
  curl -fsS -X POST "${QDRANT_URL:-http://localhost:6333}/collections/guide_chunks/snapshots" || true
fi

# ---------- 5) 清理过期备份 ----------
find "$BACKUP_DIR" -type f -mtime "+$KEEP_DAYS" -delete
echo "[$(date '+%F %T')] 完成，当前备份："
ls -lh "$BACKUP_DIR" | tail -n +2

# ================================================================================
# 恢复流程（写下来，出事时照做）
#   1) 停服务：      sudo systemctl stop travel-agent-api
#   2) 建库：        createdb travel_agent（或 docker compose up -d postgres）
#   3) 恢复 PG：     pg_restore --clean --if-exists --no-owner -d "$DB_URL" pg-<stamp>.dump
#   4) 恢复配置：    cp env-<stamp>.bak /opt/travelagent/.env && chmod 600 .env
#   5) 重建派生索引：python -m scripts.rebuild_indexes （或用 rag ingest 的 reset_derived=True）
#   6) 起服务：      sudo systemctl start travel-agent-api
#   7) 验收：        curl -fsS https://<域名>/api/health
# ================================================================================
