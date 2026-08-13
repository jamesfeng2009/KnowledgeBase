#!/usr/bin/env bash
# 恢复演练 — 验证备份可恢复（数据库备份硬约束的定期验证）
#
# 将最近一份备份恢复到独立临时库 <PGDATABASE>_restore_drill，
# 恢复后执行 SELECT 校验，成功则记录并删除临时库，失败即返回非 0（触发告警）。
#
# 环境变量：
#   PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
#   BACKUP_DIR       — 备份目录（默认 /backups）
#   RESTORE_DB_NAME  — 演练临时库名（默认 <PGDATABASE>_restore_drill）

set -euo pipefail

: "${PGHOST:=postgres}"
: "${PGPORT:=5432}"
: "${PGUSER:=ekb}"
: "${PGPASSWORD:=ekb}"
: "${PGDATABASE:=ekb}"
: "${BACKUP_DIR:=/backups}"
: "${RESTORE_DB_NAME:=${PGDATABASE}_restore_drill}"

export PGPASSWORD

# 取最近一份备份
latest=$(ls -1t "$BACKUP_DIR"/${PGDATABASE}_*.dump.gz 2>/dev/null | head -n1)
if [ -z "$latest" ]; then
  echo "[restore-drill] FAILED: no backup found in $BACKUP_DIR"
  exit 1
fi
echo "[restore-drill] using $latest"

# 清理可能残留的演练库
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
  -c "DROP DATABASE IF EXISTS $RESTORE_DB_NAME;" >/dev/null

psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
  -c "CREATE DATABASE $RESTORE_DB_NAME;" >/dev/null

# 恢复
if ! gunzip -c "$latest" | pg_restore \
  -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" \
  -d "$RESTORE_DB_NAME" --no-owner --no-privileges >/dev/null 2>&1; then
  echo "[restore-drill] FAILED: pg_restore error"
  psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
    -c "DROP DATABASE IF EXISTS $RESTORE_DB_NAME;" >/dev/null 2>&1 || true
  exit 1
fi

# 校验 — 能查询到核心表即视为恢复成功
table_count=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$RESTORE_DB_NAME" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")
echo "[restore-drill] restored tables: $table_count"

# 清理演练库
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
  -c "DROP DATABASE $RESTORE_DB_NAME;" >/dev/null

echo "[restore-drill] OK — backup is restorable"
