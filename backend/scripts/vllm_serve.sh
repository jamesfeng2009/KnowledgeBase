#!/bin/bash
# vLLM 启动脚本 — 加载 7B base + DPO LoRA adapter，暴露 OpenAI 兼容 API
#
# 用法：
#   ./vllm_serve.sh                    # 默认加载 dpo-v3-7b adapter
#   ./vllm_serve.sh /data/adapters/xxx # 指定 adapter 路径
#   VLLM_BASE_MODEL=Qwen2.5-7B-Instruct ./vllm_serve.sh
#
# 环境变量：
#   VLLM_BASE_MODEL  — base 模型路径或 HF ID（默认 Qwen2.5-7B-Instruct）
#   VLLM_LORA_PATH   — LoRA adapter 权重路径（默认 /data/adapters/dpo-7b-v3）
#   VLLM_LORA_ADAPTER — 注册的 adapter name（默认 dpo-v3-7b）
#   VLLM_PORT         — 服务端口（默认 8003）
#   VLLM_MAX_LORAS    — 最大并发 LoRA 数（默认 4，multi-tenant 场景可调大）
#
# 依赖：
#   pip install vllm>=0.6.0  # vLLM 需要 CUDA（NVIDIA GPU）
#
# multi-LoRA 路由原理：
#   vLLM 启动时通过 --lora-modules name=path 注册 adapter
#   API 请求中 model 字段传 adapter name（如 "dpo-v3-7b"）
#   vLLM 自动路由到 base + adapter，无需切换模型实例
set -euo pipefail

# === 配置 ===
BASE_MODEL="${VLLM_BASE_MODEL:-Qwen2.5-7B-Instruct}"
LORA_PATH="${VLLM_LORA_PATH:-/data/adapters/dpo-7b-v3}"
LORA_ADAPTER="${VLLM_LORA_ADAPTER:-dpo-v3-7b}"
PORT="${VLLM_PORT:-8003}"
MAX_LORAS="${VLLM_MAX_LORAS:-4}"
MAX_LORA_RANK="${VLLM_MAX_LORA_RANK:-64}"  # LoRA rank 上限，适配训练时 rank=64

echo "=========================================="
echo "vLLM 启动 — 微调模型服务"
echo "=========================================="
echo "Base model:    $BASE_MODEL"
echo "LoRA adapter:  $LORA_ADAPTER → $LORA_PATH"
echo "Port:          $PORT"
echo "Max LoRAs:     $MAX_LORAS"
echo "Max LoRA rank: $MAX_LORA_RANK"
echo "=========================================="

# 检查 adapter 权重存在
if [ ! -f "$LORA_PATH/adapter_config.json" ]; then
    echo "❌ 错误：LoRA adapter 不存在于 $LORA_PATH"
    echo "   请先训练或下载 adapter，或设置 VLLM_LORA_PATH 环境变量"
    exit 1
fi

# 检查 GPU
if ! command -v nvidia-smi &>/dev/null; then
    echo "❌ 错误：未检测到 NVIDIA GPU，vLLM 需要 CUDA"
    echo "   本机为 Apple Silicon 请在 AutoDL / 云 GPU 上运行"
    exit 1
fi

echo "GPU 信息："
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader
echo ""

# === 启动 vLLM ===
# --enable-lora: 开启 multi-LoRA 模式
# --lora-modules: 注册 adapter name=path
# --max-num-loras: 同时加载的 LoRA 数（multi-tenant 场景）
# --max-lora-rank: LoRA rank 上限（训练时 rank=64）
# --dtype bfloat16: 7B 模型用 bf16（4xx 系列 GPU 原生支持）
# --gpu-memory-utilization 0.9: 预留 10% 显存给 KV cache
exec python -m vllm.entrypoints.openai.api_server \
    --model "$BASE_MODEL" \
    --enable-lora \
    --lora-modules "$LORA_ADAPTER=$LORA_PATH" \
    --max-num-loras "$MAX_LORAS" \
    --max-lora-rank "$MAX_LORA_RANK" \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.9 \
    --port "$PORT" \
    --host 0.0.0.0 \
    --served-model-name "$LORA_ADAPTER" \
    2>&1 | tee -a /var/log/vllm_serve.log
