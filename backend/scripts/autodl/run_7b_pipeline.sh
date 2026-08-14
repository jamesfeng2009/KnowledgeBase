#!/bin/bash
# 7B 全流程训练：RLAIF 扩数据 5k → SFT → DPO → GRPO → 评测
# 在 AutoDL RTX 4090D（24GB）上运行
set -euo pipefail

PY=/root/miniconda3/envs/finetune/bin/python
MODEL_7B=/root/models/Qwen2.5-7B-Instruct
MODEL_1_5B=/root/models/Qwen2.5-1.5B-Instruct
SFT_ADAPTER_1_5B=/root/outputs/sft-v3  # 1.5B SFT adapter（已训好，RLAIF 候选生成用）
DATA_DIR=/root/data
OUT_DIR=/root/outputs

echo "============================================================"
echo "=== 7B 全流程训练 $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "=== 模型: $MODEL_7B"
echo "=== GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader)"
echo "============================================================"

# ---- [1/6] RLAIF 扩数据到 5k ----
echo ""
echo "=== [1/6] RLAIF 生成 5k 偏好对（1.5B policy + 7B judge）==="
echo "=== 1.5B 做 policy（快），7B 做 judge（准），共占 ~18GB 显存 ==="
RLAIF_OUT=$DATA_DIR/dpo_rlaif_5k.jsonl

$PY /root/KnowledgeBase/backend/scripts/finetune/generate_rlaif_data.py \
    --output "$RLAIF_OUT" \
    --count 5000 \
    --policy_model "$MODEL_1_5B" \
    --sft_adapter "$SFT_ADAPTER_1_5B" \
    --judge local \
    --local_judge_model "$MODEL_7B" \
    --max_new_tokens 128 \
    --batch_size 8 \
    --resume_keep \
    --checkpoint_every 50 \
    --seed 42 \
    2>&1 | tee /root/rlaif_5k.log

RLAIF_COUNT=$(wc -l < "$RLAIF_OUT" 2>/dev/null || echo 0)
echo "✓ RLAIF 生成完成: $RLAIF_COUNT 对 — $RLAIF_OUT"

# ---- [2/6] 合并 DPO 数据 ----
echo ""
echo "=== [2/6] 合并 DPO 数据（原始 600 + RLAIF 5k）==="
DPO_MERGED=$DATA_DIR/dpo_7b_merged.jsonl
cat "$DATA_DIR/dpo.jsonl" "$RLAIF_OUT" > "$DPO_MERGED"
TOTAL_DPO=$(wc -l < "$DPO_MERGED")
echo "✓ 合并完成: $TOTAL_DPO 对 — $DPO_MERGED"

# ---- [3/6] 7B SFT 训练 ----
echo ""
echo "=== [3/6] 7B SFT 训练（LoRA rank16 + bf16 + 3 epochs）==="
SFT_7B=$OUT_DIR/sft-7b-v3
$PY /root/KnowledgeBase/backend/scripts/finetune/train_lora.py \
    --data "$DATA_DIR/sft.jsonl" \
    --base_model "$MODEL_7B" \
    --output_dir "$SFT_7B" \
    --lora_rank 16 --lora_alpha 32 \
    --epochs 3 --batch_size 2 --grad_accum 8 \
    --max_len 2048 --lr 1e-4 \
    2>&1 | tee /root/sft_7b.log
echo "✓ SFT 完成: $SFT_7B"

# ---- [4/6] 7B DPO 训练 ----
echo ""
echo "=== [4/6] 7B DPO 训练（从 SFT adapter + 5k DPO 数据）==="
DPO_7B=$OUT_DIR/dpo-7b-v3
$PY /root/KnowledgeBase/backend/scripts/finetune/train_dpo.py \
    --data "$DPO_MERGED" \
    --base_model "$MODEL_7B" \
    --sft_adapter "$SFT_7B" \
    --output_dir "$DPO_7B" \
    --beta 0.1 --lora_rank 16 --lora_alpha 32 \
    --epochs 1 --batch_size 2 --grad_accum 8 \
    --max_len 2048 --lr 5e-5 \
    2>&1 | tee /root/dpo_7b.log
echo "✓ DPO 完成: $DPO_7B"

# ---- [5/6] 7B GRPO 冒烟训练 ----
echo ""
echo "=== [5/6] 7B GRPO 冒烟训练（30 步，从 DPO adapter）==="
GRPO_7B=$OUT_DIR/grpo-7b-v3
$PY /root/KnowledgeBase/backend/scripts/finetune/train_grpo.py \
    --data "$DPO_MERGED" \
    --base_model "$MODEL_7B" \
    --sft_adapter "$SFT_7B" \
    --output_dir "$GRPO_7B" \
    --reward_version v2 \
    --num_generations 4 \
    --generation_batch_size 4 \
    --max_completion_length 128 \
    --temperature 1.0 \
    --beta 0.04 --lr 1e-6 \
    --max_steps 30 \
    --limit 200 \
    2>&1 | tee /root/grpo_7b.log
echo "✓ GRPO 完成: $GRPO_7B"

# ---- [6/6] 评测 ----
echo ""
echo "=== [6/6] 模型级边界拒答评测 ==="
for STAGE in "base:none" "sft:$SFT_7B" "dpo:$DPO_7B" "grpo:$GRPO_7B"; do
    LABEL="${STAGE%%:*}"
    ADAPTER="${STAGE##*:}"
    echo ""
    echo "======== 7B $LABEL ========"
    if [ "$ADAPTER" = "none" ]; then
        $PY /root/KnowledgeBase/backend/scripts/finetune/eval_boundary_200.py \
            --base_model "$MODEL_7B" --label "7B-$LABEL" 2>&1 | grep -E "^\[|^=|拒答|误拒答|汇总"
    else
        $PY /root/KnowledgeBase/backend/scripts/finetune/eval_boundary_200.py \
            --base_model "$MODEL_7B" --adapter "$ADAPTER" --label "7B-$LABEL" 2>&1 | grep -E "^\[|^=|拒答|误拒答|汇总"
    fi
done

echo ""
echo "================================================================"
echo "  ✅ 7B 全流程训练完成"
echo "  SFT:  $SFT_7B"
echo "  DPO:  $DPO_7B"
echo "  GRPO: $GRPO_7B"
echo "  DPO 数据: $TOTAL_DPO 对"
echo "================================================================"
