#!/usr/bin/env bash
# 备份容器入口 — 安装 cron 并启动定时任务。
#
# 调度：
#   BACKUP_CRON      — 备份 cron 表达式（默认每日 03:00 UTC）
#   RESTORE_CRON     — 恢复演练 cron 表达式（默认每周日 05:00 UTC）
#   BACKUP_SCRIPT    — 备份脚本路径（默认 /scripts/backup.sh）
#   RESTORE_SCRIPT   — 恢复演练脚本路径（默认 /scripts/restore_drill.sh）

set -euo pipefail

: "${BACKUP_CRON:=0 3 * * *}"
: "${RESTORE_CRON:=0 5 * * 0}"
: "${BACKUP_SCRIPT:=/scripts/backup.sh}"
: "${RESTORE_SCRIPT:=/scripts/restore_drill.sh}"

chmod +x "$BACKUP_SCRIPT" "$RESTORE_SCRIPT"

# 写入 cron 配置
cat > /etc/cron.d/ekb-backup <<EOF
# 数据库定时备份（硬约束）
$BACKUP_CRON root bash $BACKUP_SCRIPT >> /var/log/backup.log 2>&1
# 恢复演练 — 定期验证备份可恢复
$RESTORE_CRON root bash $RESTORE_SCRIPT >> /var/log/restore_drill.log 2>&1
EOF
chmod 0644 /etc/cron.d/ekb-backup

# 启动 cron
exec cron -f -L 15
