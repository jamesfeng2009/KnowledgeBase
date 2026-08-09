#!/usr/bin/env python
"""LoRA / QLoRA SFT 训练脚本 — 企业知识库问答模型指令微调。

输入数据（与后端导出 pipeline 对齐的 sft.jsonl）：
    {"messages":[{"role":"system","content":"..."},{"role":"user","content":"..."},
                 {"role":"assistant","content":"..."}],
     "meta":{"source":"...","doc_ids":[...],"tenant_id":"..."}}

功能：
    - messages 经 tokenizer.apply_chat_template 渲染为训练文本（无模板时回退纯文本拼接）
    - LoRA（bf16）/ QLoRA（4bit NF4 + 双重量化）两种模式
    - 留出 5% 样本作为验证集，按 epoch 计算 eval loss
    - 训练结束保存 LoRA adapter + tokenizer，并打印完整可复现信息（超参 / 样本数 / seed）

依赖安装（独立 ML 工具链，不写入项目 requirements.txt）：
    pip install "torch>=2.2" "transformers>=4.45" "peft>=0.13" "trl>=0.16" \
                "datasets>=2.20" "accelerate>=0.34" "bitsandbytes>=0.43"  # bitsandbytes 仅 QLoRA 需要

运行示例：
    # QLoRA（单卡 24GB 可训 7B）
    python scripts/finetune/train_lora.py \
        --data data/sft.jsonl --output_dir outputs/lora-sft --qlora \
        --epochs 3 --batch_size 4 --grad_accum 8

    # LoRA（bf16 全精度底座，建议 A100/H100）
    python scripts/finetune/train_lora.py \
        --data data/sft.jsonl --base_model Qwen/Qwen2.5-7B-Instruct \
        --output_dir outputs/lora-sft --lora_rank 16 --lora_alpha 32 --lr 1e-4

Mac（Apple Silicon）注意：
    - QLoRA 不可用（bitsandbytes 不支持 MPS），请用默认 LoRA 模式；
      PyTorch 2.3+ / macOS 14+ 会自动检测并启用 MPS bf16（比 fp16 训练更稳）
    - 建议先用 Qwen2.5-1.5B/3B + --batch_size 1 --grad_accum 8 --max_len 1024 跑通；
      7B 可跑但较慢，追求速度可改用 mlx-lm（Apple 原生框架）
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("train_lora")

#: LoRA 注入的目标模块（Qwen2.5 / Llama 系架构通用的全量线性层）
DEFAULT_TARGET_MODULES = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
]


def iter_jsonl(path: str | Path) -> Iterator[tuple[int, dict]]:
    """逐行读取 JSONL，跳过坏行（JSON 解析失败 / 非对象行）。Yields (行号, dict)。"""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("跳过坏行 %s:%d — JSON 解析失败: %s", path, lineno, exc)
                continue
            if not isinstance(obj, dict):
                logger.warning("跳过坏行 %s:%d — 顶层不是 JSON 对象", path, lineno)
                continue
            yield lineno, obj


def _validate_messages(messages: Any) -> bool:
    """校验 messages 结构：非空列表、每条含 str 类型的 role/content、至少一对 user/assistant。"""
    if not isinstance(messages, list) or len(messages) < 2:
        return False
    roles = []
    for m in messages:
        if not isinstance(m, dict):
            return False
        if not isinstance(m.get("role"), str) or not isinstance(m.get("content"), str):
            return False
        if not m["content"].strip():
            return False
        roles.append(m["role"])
    return "user" in roles and "assistant" in roles


def load_sft_jsonl(path: str | Path) -> list[dict]:
    """加载 sft.jsonl，返回 [{"messages": [...], "meta": {...}}, ...]。

    坏行 / 结构不合法的样本跳过并记 warning。可独立导入测试（仅依赖标准库）。
    """
    records: list[dict] = []
    for lineno, obj in iter_jsonl(path):
        if not _validate_messages(obj.get("messages")):
            logger.warning("跳过样本 %s:%d — messages 结构不合法", path, lineno)
            continue
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        records.append({"messages": obj["messages"], "meta": meta})
    logger.info("加载 SFT 样本 %d 条 — %s", len(records), path)
    return records


def render_chat(messages: list[dict], tokenizer: Any | None = None) -> str:
    """将 messages 渲染为单条训练文本。

    优先使用 tokenizer.apply_chat_template（与基座模型官方模板严格一致）；
    tokenizer 缺失或无模板时回退为 ``<|role|>\\ncontent`` 纯文本拼接（便于离线单测）。
    """
    if tokenizer is not None and getattr(tokenizer, "chat_template", None):
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    return "\n".join(f"<|{m['role']}|>\n{m['content']}" for m in messages)


def build_hf_dataset(records: list[dict], tokenizer: Any, eval_ratio: float, seed: int):
    """records -> datasets.DatasetDict(train/test)，文本列名为 ``text``。

    重依赖 datasets 在此函数内延迟导入。
    """
    from datasets import Dataset  # 延迟导入：无 GPU 环境也可 import 本模块

    rows = [{"text": render_chat(r["messages"], tokenizer)} for r in records]
    ds = Dataset.from_list(rows)
    if len(ds) < 2:
        raise ValueError(f"样本过少（{len(ds)} 条），无法划分训练/验证集，请至少提供数十条样本")
    test_size = max(1, round(len(ds) * eval_ratio))
    return ds.train_test_split(test_size=test_size, seed=seed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LoRA/QLoRA SFT 训练（transformers + peft + trl SFTTrainer）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, help="sft.jsonl 路径")
    parser.add_argument("--base_model", default="Qwen/Qwen2.5-7B-Instruct", help="基座模型（HF repo 或本地路径）")
    parser.add_argument("--output_dir", default="outputs/lora-sft", help="LoRA adapter 输出目录")
    parser.add_argument("--lora_rank", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha（缩放 = alpha/rank）")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout")
    parser.add_argument("--lr", type=float, default=1e-4, help="学习率")
    parser.add_argument("--epochs", type=float, default=3, help="训练轮数")
    parser.add_argument("--batch_size", type=int, default=4, help="单卡 micro batch size")
    parser.add_argument("--grad_accum", type=int, default=8, help="梯度累积步数（等效 batch = batch_size × grad_accum）")
    parser.add_argument("--qlora", action="store_true", help="启用 4bit NF4 量化（QLoRA，需要 bitsandbytes）")
    parser.add_argument("--max_len", type=int, default=2048, help="最大序列长度（超出截断）")
    parser.add_argument("--eval_ratio", type=float, default=0.05, help="验证集比例")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--no_gradient_checkpointing", action="store_true",
                        help="关闭梯度检查点。1.5B 小模型推荐开启（实测内存不变快40%%）；"
                             "7B 勿用——权重占大头，关 ckpt 激活翻倍会 OOM。见微调.md 附录D")
    return parser.parse_args(argv)


def _detect_bf16() -> bool:
    """检测当前训练设备是否支持 bf16（CUDA 或 Apple Silicon MPS）。

    PyTorch 2.3+ / macOS 14+ 的 MPS 后端已支持 bf16，训练比 fp16 更稳
    （fp16 易梯度溢出）。用实际分配张量探测，比按版本号判断更可靠。
    """
    import torch  # 延迟导入，保持无 GPU 环境可 import 本模块

    if torch.cuda.is_available():
        return bool(torch.cuda.is_bf16_supported())
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        try:
            torch.zeros(1, dtype=torch.bfloat16, device="mps")
            return True
        except Exception:  # 旧版 PyTorch/macOS 的 MPS 不支持 bf16
            return False
    return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # ---- 重依赖延迟导入：保证无 GPU 环境下 import 本模块（做单测）不报错 ----
    import torch
    from peft import LoraConfig, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    # ---- 可复现性：固定随机种子 ----
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    # ---- 数据 ----
    records = load_sft_jsonl(args.data)

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    split = build_hf_dataset(records, tokenizer, eval_ratio=args.eval_ratio, seed=args.seed)
    train_ds, eval_ds = split["train"], split["test"]
    logger.info("训练集 %d 条 / 验证集 %d 条", len(train_ds), len(eval_ds))

    # ---- 模型（QLoRA 4bit 或 LoRA bf16）----
    use_bf16 = _detect_bf16()
    quantization_config = None
    if args.qlora:
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if use_bf16 else torch.float16,
        )
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=quantization_config,
        torch_dtype=torch.bfloat16 if use_bf16 else torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False  # 训练时关闭 KV cache，配合 gradient checkpointing 省显存
    if args.qlora:
        model = prepare_model_for_kbit_training(model)

    peft_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=DEFAULT_TARGET_MODULES,
    )

    # ---- 训练参数（SFTConfig 继承 TrainingArguments，附加 SFT 专有字段）----
    train_args = SFTConfig(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        logging_steps=10,
        eval_strategy="epoch",  # 每个 epoch 在 5% 验证集上算 eval loss
        save_strategy="epoch",
        save_total_limit=2,
        bf16=use_bf16,
        fp16=(not use_bf16) and torch.cuda.is_available(),
        gradient_checkpointing=not args.no_gradient_checkpointing,
        max_length=args.max_len,
        packing=False,
        dataset_text_field="text",
        # 注：trl>=0.16 支持 assistant_only_loss=True 仅对 assistant 段计 loss，
        # 需基座 chat template 含 {% generation %} 标记；Qwen2.5 模板不支持，故用全序列 loss。
        report_to=[],  # 面试作品集：默认不接 wandb/mlflow，需要时自行打开
        seed=args.seed,
    )

    trainer = SFTTrainer(
        model=model,
        args=train_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
        peft_config=peft_config,
    )

    # ---- 可复现信息打印 ----
    repro_info = {
        "base_model": args.base_model,
        "mode": "qlora-4bit" if args.qlora else "lora-bf16",
        "lora": {"rank": args.lora_rank, "alpha": args.lora_alpha, "dropout": args.lora_dropout,
                 "target_modules": DEFAULT_TARGET_MODULES},
        "lr": args.lr,
        "epochs": args.epochs,
        "effective_batch_size": args.batch_size * args.grad_accum,
        "max_len": args.max_len,
        "gradient_checkpointing": not args.no_gradient_checkpointing,
        "seed": args.seed,
        "num_train_samples": len(train_ds),
        "num_eval_samples": len(eval_ds),
        "torch_version": torch.__version__,
        "cuda": torch.version.cuda if torch.cuda.is_available() else None,
    }
    logger.info("可复现信息:\n%s", json.dumps(repro_info, ensure_ascii=False, indent=2))

    trainable = sum(p.numel() for p in trainer.model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in trainer.model.parameters())
    logger.info("可训练参数: %s / %s (%.4f%%)", f"{trainable:,}", f"{total:,}", 100 * trainable / total)

    # ---- 训练 & 保存 ----
    trainer.train()
    trainer.save_model(args.output_dir)  # 保存 LoRA adapter（adapter_model.safetensors + config）
    tokenizer.save_pretrained(args.output_dir)
    logger.info("LoRA adapter 与 tokenizer 已保存至 %s", args.output_dir)

    eval_metrics = trainer.evaluate()
    logger.info("最终验证集指标: %s", eval_metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
