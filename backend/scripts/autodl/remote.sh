#!/bin/bash
# AutoDL 实例远程命令执行（sshpass 自动填密码，最可靠）
#
# 用法：
#   ./remote.sh '远程命令'              # 执行并返回
#   ./remote.sh 'bash -s' < setup.sh    # 通过 stdin 传脚本到远程执行
#
# 依赖：同目录 instance.conf（已 gitignore，含 AUTODL_SSH_HOST/PORT/PASS）
#       sshpass（macOS: brew install sshpass / 已预装）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$SCRIPT_DIR/instance.conf"
if [ ! -f "$CONF" ]; then
    echo "错误：缺少 $CONF（含 SSH 连接信息）" >&2
    echo "请先运行：python $(dirname "$0")/manage.py detail <instance_uuid>" >&2
    exit 1
fi
source "$CONF"

CMD="${1:-}"
if [ -z "$CMD" ]; then
    echo "用法: $0 '远程命令'" >&2
    echo "      $0 'bash -s' < setup.sh   # stdin 传脚本" >&2
    exit 1
fi

if ! command -v sshpass &>/dev/null; then
    echo "错误：需要 sshpass（macOS: brew install sshpass）" >&2
    exit 1
fi

# sshpass 自动填密码，比 SSH_ASKPASS 更可靠（不依赖 OpenSSH 版本/tty 行为）
# ConnectTimeout=15: 防止连接挂死；ServerAliveInterval=30: 保活
exec sshpass -p "$AUTODL_SSH_PASS" ssh -p "$AUTODL_SSH_PORT" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=15 -o ServerAliveInterval=30 \
  root@"$AUTODL_SSH_HOST" "$CMD"
