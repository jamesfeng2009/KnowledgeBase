#!/bin/bash
# AutoDL 远程训练全流程：SFT → DPO → GRPO + 评测
# 在远程实例 root 下执行：bash /root/KnowledgeBase/backend/scripts/autodl/run_pipeline.sh
#
# 环境前提（setup_remote.sh 已完成）：
#   - conda env finetune (Python 3.10, torch 2.x+cu118, trl, peft, transformers)
#   - 模型 /root/models/Qwen2.5-1.5B-Instruct
#   - 代码 /root/KnowledgeBase/backend/scripts/finetune/
set -euo pipefail

PY=/root/miniconda3/envs/finetune/bin/python
SCRIPTS=/root/KnowledgeBase/backend/scripts/finetune
MODEL=/root/models/Qwen2.5-1.5B-Instruct
DATA=/root/data
OUT=/root/outputs

mkdir -p "$DATA" "$OUT"
cd "$SCRIPTS"

export HF_HUB_DISABLE_XET=1
export HF_ENDPOINT=https://hf-mirror.com
export TOKENIZERS_PARALLELISM=false

echo "================================================================"
echo "  企业知识库模型训练全流程"
echo "  模型: $MODEL"
echo "  开始时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"

# ================================================================
# P0-1: 生成 SFT 数据
# ================================================================
echo ""
echo "=== [1/7] 生成 SFT 训练数据 ==="
$PY generate_sft_data.py --output "$DATA/sft.jsonl" --count 800 --seed 42
echo "✓ SFT 数据生成完成: $(wc -l < "$DATA/sft.jsonl") 条"

# ================================================================
# P0-2: 训练 SFT (LoRA)
# ================================================================
echo ""
echo "=== [2/7] 训练 SFT (LoRA) ==="
$PY train_lora.py \
    --data "$DATA/sft.jsonl" \
    --base_model "$MODEL" \
    --output_dir "$OUT/sft-v3" \
    --lora_rank 16 --lora_alpha 32 \
    --lr 1e-4 --epochs 3 \
    --batch_size 8 --grad_accum 4 \
    --max_len 2048 --eval_ratio 0.05 \
    --seed 42
echo "✓ SFT 训练完成: $OUT/sft-v3"

# ================================================================
# P0-3: 生成 DPO 数据
# ================================================================
echo ""
echo "=== [3/7] 生成 DPO 偏好对齐数据 ==="
$PY generate_dpo_data.py --output "$DATA/dpo.jsonl" --count 600 --seed 42
echo "✓ DPO 数据生成完成: $(wc -l < "$DATA/dpo.jsonl") 条"

# ================================================================
# P0-4: 训练 DPO
# ================================================================
echo ""
echo "=== [4/7] 训练 DPO（偏好对齐）==="
$PY train_dpo.py \
    --data "$DATA/dpo.jsonl" \
    --base_model "$MODEL" \
    --sft_adapter "$OUT/sft-v3" \
    --output_dir "$OUT/dpo-v3" \
    --beta 0.1 --lr 5e-5 --epochs 1 \
    --batch_size 4 --grad_accum 8 \
    --max_len 2048 \
    --seed 42
echo "✓ DPO 训练完成: $OUT/dpo-v3"

# ================================================================
# P0-5: 生成 RLAIF 数据（规则裁判，零 API 成本）
# ================================================================
echo ""
echo "=== [5/7] 生成 RLAIF 数据（规则裁判）==="
$PY generate_rlaif_data.py \
    --output "$DATA/dpo_rlaif.jsonl" \
    --count 500 \
    --judge rule \
    --policy_model "$MODEL" \
    --sft_adapter "$OUT/sft-v3" \
    --checkpoint_every 100 \
    --seed 42
echo "✓ RLAIF 数据生成完成: $(wc -l < "$DATA/dpo_rlaif.jsonl") 条"

# ================================================================
# P0-6: 训练 GRPO（强化学习）
# ================================================================
echo ""
echo "=== [6/7] 训练 GRPO（强化学习）==="
$PY train_grpo.py \
    --data "$DATA/dpo_rlaif.jsonl" \
    --base_model "$MODEL" \
    --sft_adapter "$OUT/sft-v3" \
    --output_dir "$OUT/grpo-v3" \
    --lr 1e-5 --epochs 1 \
    --batch_size 4 --grad_accum 4 \
    --max_len 2048 \
    --seed 42
echo "✓ GRPO 训练完成: $OUT/grpo-v3"

# ================================================================
# P0-7: 评测验证 13 项修复
# ================================================================
echo ""
echo "=== [7/7] 评测验证 ==="
$PY -m pytest "$SCRIPTS/../tests/test_finetune_scripts.py" -v 2>&1 | tail -30

echo ""
echo "================================================================"
echo "  ✅ 全流程训练完成"
echo "  结束时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  SFT adapter:  $OUT/sft-v3"
echo "  DPO adapter:  $OUT/dpo-v3"
echo "  GRPO adapter: $OUT/grpo-v3"
echo "================================================================"
