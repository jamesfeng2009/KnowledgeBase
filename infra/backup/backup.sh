#!/usr/bin/env bash
# 定时 PostgreSQL 备份 — 数据库备份（硬约束）
#
# 使用 pg_dump（custom 格式）生成带时间戳的备份文件并 gzip 压缩，
# 按 RETENTION_DAYS 保留最近备份、清理过期文件。
# 可选：BACKUP_S3_ENDPOINT 配置时用 aws-cli 上传到 S3/MinIO 对象存储。
#
# 环境变量：
#   PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE  — 目标库连接
#   BACKUP_DIR            — 备份输出目录（默认 /backups）
#   RETENTION_DAYS        — 保留天数（默认 14）
#   BACKUP_S3_ENDPOINT    — 可选，S3/MinIO 端点（含 bucket，如 s3://ekb-backups）
#   BACKUP_S3_ENDPOINT_URL— 可选，S3 兼容存储地址（MinIO 需设置）

set -euo pipefail

: "${PGHOST:=postgres}"
: "${PGPORT:=5432}"
: "${PGUSER:=ekb}"
: "${PGPASSWORD:=ekb}"
: "${PGDATABASE:=ekb}"
: "${BACKUP_DIR:=/backups}"
: "${RETENTION_DAYS:=14}"

mkdir -p "$BACKUP_DIR"
ts=$(date +%Y%m%d_%H%M%S)
backup_file="$BACKUP_DIR/${PGDATABASE}_${ts}.dump"
backup_gz="$backup_file.gz"

export PGPASSWORD
export PGOPTIONS="${PGOPTIONS:--c default_transaction_read_only=on}"

echo "[backup] start $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# custom 格式 + 压缩，兼容 pg_restore 恢复演练
pg_dump \
  -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" \
  -d "$PGDATABASE" \
  -Fc --no-owner --no-privileges \
  -f "$backup_file"

gzip -f "$backup_file"

# 校验备份文件非空
if [ ! -s "$backup_gz" ]; then
  echo "[backup] FAILED: empty backup file $backup_gz"
  exit 1
fi

# 校验 dump 完整性（list 能读即为有效）
gunzip -c "$backup_gz" | pg_restore --list >/dev/null 2>&1 \
  || { echo "[backup] FAILED: invalid dump"; rm -f "$backup_gz"; exit 1; }

# 清理过期备份
find "$BACKUP_DIR" -name "${PGDATABASE}_*.dump.gz" -mtime +"$RETENTION_DAYS" -delete

# 可选上传对象存储（S3/MinIO）
if [ -n "${BACKUP_S3_ENDPOINT:-}" ]; then
  if command -v aws >/dev/null 2>&1; then
    aws_args=(s3 cp "$backup_gz" "$BACKUP_S3_ENDPOINT/")
    [ -n "${BACKUP_S3_ENDPOINT_URL:-}" ] && aws_args+=(--endpoint-url "$BACKUP_S3_ENDPOINT_URL")
    aws "${aws_args[@]}" >/dev/null 2>&1 \
      && echo "[backup] uploaded to $BACKUP_S3_ENDPOINT" \
      || echo "[backup] WARN: S3 upload failed"
  else
    echo "[backup] WARN: aws cli not found, skip S3 upload"
  fi
fi

echo "[backup] done ${backup_gz}"
