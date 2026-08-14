#!/bin/bash
# ORPO 7B 训练完成后自动评测监控脚本
# 等待训练进程 PID=546 结束 → 检查 adapter → 运行弱 prompt 评测 → 写日志
set -u
cd /Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend
LOG=/Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/outputs/orpo-v1-7b-eval.log
PID=546

echo "$(date '+%F %T'): 监控启动，等待训练进程 PID=$PID 结束..." > "$LOG"

# 等待训练进程结束（每 90s 探活一次）
while kill -0 "$PID" 2>/dev/null; do
  sleep 90
done

echo "$(date '+%F %T'): 训练进程已结束" >> "$LOG"

# 检查 adapter 目录
ADAPTER=outputs/orpo-v1-7b
echo "$(date '+%F %T'): adapter 目录内容:" >> "$LOG"
ls -la "$ADAPTER"/ >> "$LOG" 2>&1

if [ ! -d "$ADAPTER" ] || [ -z "$(ls -A "$ADAPTER" 2>/dev/null)" ]; then
  echo "$(date '+%F %T'): [错误] adapter 目录 $ADAPTER 不存在或为空，训练可能未正常保存" >> "$LOG"
  exit 1
fi

echo "$(date '+%F %T'): 开始运行弱 prompt 评测..." >> "$LOG"
source .venv/bin/activate
python scripts/finetune/eval_boundary_200.py \
  --base_model models/Qwen2.5-7B-Instruct --adapter "$ADAPTER" >> "$LOG" 2>&1
EVAL_RC=$?

echo "$(date '+%F %T'): 评测完成，退出码=$EVAL_RC" >> "$LOG"
