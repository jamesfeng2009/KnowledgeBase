#!/bin/bash
# AutoDL 实例环境初始化（在远程 root 执行）
# 传输方式：./remote.sh 'bash -s' < setup_remote.sh
#
# 镜像 base-image-l2t43iu6uk 自带 Python 3.8 + torch 2.0+cu118，
# 但 trl>=0.16 要求 Python>=3.10，故创建独立 conda 环境 finetune。
set -e

# conda 初始化（非交互 shell 需手动 source）
__conda_setup="$(/root/miniconda3/bin/conda shell.bash hook 2>/dev/null)"
[ -n "$__conda_setup" ] && eval "$__conda_setup"

ENV_NAME=finetune
PY=/root/miniconda3/envs/$ENV_NAME/bin/python
PIP=/root/miniconda3/envs/$ENV_NAME/bin/pip

echo "=== 1. 系统信息 ==="
nvidia-smi -L
df -h / | tail -1

echo ""
echo "=== 2. 创建 conda 环境（Python 3.10）==="
if conda env list | grep -q "^$ENV_NAME "; then
    echo "环境 $ENV_NAME 已存在"
else
    conda create -n $ENV_NAME python=3.10 -y
fi
$PY --version

echo ""
echo "=== 3. clone 代码（gitee 优先，github 兜底）==="
cd /root
if [ -d KnowledgeBase ]; then
    echo "目录已存在，拉取最新..."
    cd KnowledgeBase && git pull --ff-only || echo "pull 失败，用现有版本"
else
    git clone --depth 1 https://gitee.com/globalization/enterprise-knowledge.git KnowledgeBase \
        || git clone --depth 1 https://github.com/jamesfeng2009/KnowledgeBase.git KnowledgeBase
    cd KnowledgeBase
fi
echo "代码版本：$(git rev-parse --short HEAD)  $(git log -1 --format=%s)"
if [ -f backend/scripts/finetune/finetune_utils.py ]; then
    echo "✓ finetune_utils.py 存在（13 项修复已同步）"
else
    echo "✗ finetune_utils.py 缺失，代码版本不对"; exit 1
fi

echo ""
echo "=== 4. 装训练依赖 ==="
$PIP install -q --upgrade pip
# torch 2.2+cuda118（镜像自带 2.0，升级以满足 trl>=0.16 的 torch>=2.2 要求）
$PIP install -q --upgrade "torch>=2.2" --index-url https://download.pytorch.org/whl/cu118
$PIP install -q "transformers>=4.45" "trl>=0.16" "peft>=0.13" \
    "datasets>=2.20" "accelerate>=0.34" "bitsandbytes>=0.43" "sentence-transformers" \
    "huggingface_hub"

echo ""
echo "=== 5. 验证 CUDA + 依赖 ==="
$PY -c "
import torch, transformers, trl, peft, datasets
print(f'torch {torch.__version__} | cuda={torch.cuda.is_available()} | '
      f'{torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"NO GPU\"}')
print(f'transformers {transformers.__version__} | trl {trl.__version__} | peft {peft.__version__}')
assert torch.cuda.is_available(), 'CUDA 不可用！'
print('✓ 环境验证通过')
"

echo ""
echo "=== 6. 下载 Qwen2.5-1.5B-Instruct ==="
mkdir -p /root/models
if [ -d /root/models/Qwen2.5-1.5B-Instruct ] && [ "$(ls -A /root/models/Qwen2.5-1.5B-Instruct 2>/dev/null)" ]; then
    echo "模型已存在，跳过"
else
    HF_ENDPOINT=https://hf-mirror.com /root/miniconda3/envs/$ENV_NAME/bin/huggingface-cli download \
        Qwen/Qwen2.5-1.5B-Instruct --local-dir /root/models/Qwen2.5-1.5B-Instruct
fi
ls /root/models/Qwen2.5-1.5B-Instruct/

echo ""
echo "=== 环境初始化完成 ==="
echo "代码：/root/KnowledgeBase（$(git -C /root/KnowledgeBase rev-parse --short HEAD)）"
echo "Python：$PY"
echo "模型：/root/models/Qwen2.5-1.5B-Instruct"
echo ""
echo "下一步："
echo "  1. 生成训练数据：\$PY /root/KnowledgeBase/backend/scripts/finetune/generate_sft_data.py --output /root/data/sft.jsonl"
echo "  2. 训练 SFT：\$PY /root/KnowledgeBase/backend/scripts/finetune/train_lora.py --data /root/data/sft.jsonl --base_model /root/models/Qwen2.5-1.5B-Instruct"
