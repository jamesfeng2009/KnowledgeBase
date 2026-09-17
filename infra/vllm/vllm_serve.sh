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
#
# KV cache / prefix cache 租户（业务域）隔离原理：
#   vLLM 的 prefix caching 以 (token 序列, model 名, adapter 名) 为缓存 key
#   命中条件 — 相同 adapter 的同前缀请求复用 KV block；跨 adapter 互不复用。
#   因此「一个租户/业务域 = 一个 adapter name」即可从机制上隔离 KV cache：
#   - 租户 A 的前缀命中永远不会复用租户 B 的 KV block（防跨租户上下文泄漏）
#   - 同租户内 system prompt / few-shot 前缀正常复用，TTFT 收益保留
#   注意：base 权重在物理上共享（省显存），隔离发生在 KV 层而非权重层。
#
# 多 adapter 注册（multi-tenant 示例）：
#   VLLM_LORA_MODULES="tenant-a=/data/adapters/dpo-7b-v3 tenant-b=/data/adapters/hr-v1" \
#     ./vllm_serve.sh
#   对应 app/llm/factory.py 的 get_llm_provider_by_model：model 字段传 name。
set -euo pipefail

# === 配置 ===
BASE_MODEL="${VLLM_BASE_MODEL:-Qwen2.5-7B-Instruct}"
LORA_PATH="${VLLM_LORA_PATH:-/data/adapters/dpo-7b-v3}"
LORA_ADAPTER="${VLLM_LORA_ADAPTER:-dpo-v3-7b}"
# 多 adapter 注册：空格分隔的 "name=path" 列表（覆盖单 adapter 配置）
LORA_MODULES="${VLLM_LORA_MODULES:-}"
PORT="${VLLM_PORT:-8003}"
MAX_LORAS="${VLLM_MAX_LORAS:-4}"
MAX_LORA_RANK="${VLLM_MAX_LORA_RANK:-64}"  # LoRA rank 上限，适配训练时 rank=64

echo "=========================================="
echo "vLLM 启动 — 微调模型服务"
echo "=========================================="
echo "Base model:    $BASE_MODEL"
if [ -n "$LORA_MODULES" ]; then
    echo "LoRA modules:  $LORA_MODULES"
else
    echo "LoRA adapter:  $LORA_ADAPTER → $LORA_PATH"
fi
echo "Port:          $PORT"
echo "Max LoRAs:     $MAX_LORAS"
echo "Max LoRA rank: $MAX_LORA_RANK"
echo "=========================================="

# 检查 adapter 权重存在（单 adapter 模式）
if [ -z "$LORA_MODULES" ] && [ ! -f "$LORA_PATH/adapter_config.json" ]; then
    echo "❌ 错误：LoRA adapter 不存在于 $LORA_PATH"
    echo "   请先训练或下载 adapter，或设置 VLLM_LORA_PATH 环境变量"
    exit 1
fi

# 组装 --lora-modules 参数
LORA_ARGS=()
if [ -n "$LORA_MODULES" ]; then
    # 多 adapter：每个 "name=path" 一个参数
    for module in $LORA_MODULES; do
        LORA_ARGS+=("$module")
    done
else
    # 单 adapter（兼容旧行为）
    LORA_ARGS+=("$LORA_ADAPTER=$LORA_PATH")
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
# --lora-modules: 注册 adapter name=path（支持多个，按租户/业务域隔离）
# --max-num-loras: 同时加载的 LoRA 数（multi-tenant 场景）
# --max-lora-rank: LoRA rank 上限（训练时 rank=64）
# --enable-prefix-caching: 复用同前缀 KV block（system prompt 等），
#   缓存 key 含 adapter name → 跨租户/跨 adapter KV 天然隔离（见文件头说明）。
#   注：vLLM v0.9+ 默认开启，显式声明以保证旧版本行为一致。
# --no-enable-prefix-caching 反例：关闭后 TTFT 显著上升，压测见 scripts/bench_ttft.py
# --dtype bfloat16: 7B 模型用 bf16（4xx 系列 GPU 原生支持）
# --gpu-memory-utilization 0.9: 预留 10% 显存给 KV cache
# --served-model-name: 单 adapter 模式下对外只暴露 adapter name，
#   使 app/llm/factory.py 的 private_finetuned（model=VLLM_LORA_ADAPTER）
#   直接路由到 base+adapter；多 adapter 模式不指定，vLLM 自动暴露全部名称。
SERVED_NAME_ARGS=()
if [ -z "$LORA_MODULES" ]; then
    SERVED_NAME_ARGS+=(--served-model-name "$LORA_ADAPTER")
fi

exec python -m vllm.entrypoints.openai.api_server \
    --model "$BASE_MODEL" \
    --enable-lora \
    --lora-modules "${LORA_ARGS[@]}" \
    --max-num-loras "$MAX_LORAS" \
    --max-lora-rank "$MAX_LORA_RANK" \
    --enable-prefix-caching \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.9 \
    --port "$PORT" \
    --host 0.0.0.0 \
    "${SERVED_NAME_ARGS[@]}" \
    2>&1 | tee -a /var/log/vllm_serve.log
