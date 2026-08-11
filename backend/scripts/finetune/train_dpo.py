#!/usr/bin/env python
"""DPO 偏好对齐训练脚本 — 在 SFT 模型之上做偏好优化（trl DPOTrainer）。

输入数据（conversational 格式，trl 原生支持）：
    {"prompt":[{"role":"system","content":"..."},{"role":"user","content":"..."}],
     "chosen":[{"role":"assistant","content":"好答案"}],
     "rejected":[{"role":"assistant","content":"坏答案"}],
     "meta":{...}}

训练策略：
    - conversational 格式：trl DPOTrainer 自动检测并通过 apply_chat_template 渲染
    - 支持两种起点：
        a) 直接从 --base_model 开始（基座上新建 LoRA）
        b) 指定 --sft_adapter：先把 SFT LoRA 合并进基座（merge_and_unload），
           再在其上新建 LoRA 做 DPO —— 标准 "SFT -> DPO" 两阶段流水线
    - ref_model=None：peft 模式下 DPOTrainer 自动以"禁用 adapter 的基座"作为参考模型
    - 留出 5% 验证集，按 epoch 输出 eval rewards/accuracies

依赖安装（独立 ML 工具链，不写入项目 requirements.txt）：
    pip install "torch>=2.2" "transformers>=4.45" "peft>=0.13" "trl>=0.16" \
                "datasets>=2.20" "accelerate>=0.34" "bitsandbytes>=0.43"  # bitsandbytes 仅 --qlora 需要

运行示例：
    python scripts/finetune/train_dpo.py \
        --data data/open/dpo.jsonl --base_model models/Qwen2.5-1.5B-Instruct \
        --sft_adapter outputs/sft-v3-distill --output_dir outputs/dpo-v1-1.5b \
        --beta 0.1 --lr 5e-5 --epochs 1
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("train_dpo")

# 分组切分与 MPS 缓存清理回调统一收敛到 finetune_utils（评审 #2/#10）
from finetune_utils import (
    grouped_split_indices,
    last_user_content,
    make_mps_cache_cleanup_callback,
    question_group_key,
)

#: LoRA 注入的目标模块（与 train_lora.py 保持一致）
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
    """校验消息列表：非空 list，每条含 str 类型的 role/content。"""
    if not isinstance(messages, list) or len(messages) == 0:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            return False
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(role, str) or not isinstance(content, str) or not content.strip():
            return False
    return True


def load_dpo_jsonl(path: str | Path) -> list[dict]:
    """加载 dpo.jsonl（conversational 格式），返回 [{"prompt":[...], "chosen":[...], "rejected":[...], "meta":{...}}]。

    校验规则：
    - prompt/chosen/rejected 均为非空消息列表（每条含 role/content）
    - chosen 与 rejected 的 content 不能相同（无偏好信号，跳过）
    可独立导入测试（仅依赖标准库）。
    """
    records: list[dict] = []
    for lineno, obj in iter_jsonl(path):
        prompt, chosen, rejected = obj.get("prompt"), obj.get("chosen"), obj.get("rejected")
        if not all(_validate_messages(x) for x in (prompt, chosen, rejected)):
            logger.warning("跳过样本 %s:%d — prompt/chosen/rejected 必须为非空消息列表", path, lineno)
            continue
        chosen_text = chosen[-1]["content"].strip()
        rejected_text = rejected[-1]["content"].strip()
        if chosen_text == rejected_text:
            logger.warning("跳过样本 %s:%d — chosen 与 rejected 相同，无偏好信号", path, lineno)
            continue
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        records.append({"prompt": prompt, "chosen": chosen, "rejected": rejected, "meta": meta})
    logger.info("加载 DPO 样本 %d 条 — %s", len(records), path)
    return records


def build_hf_dataset(records: list[dict], eval_ratio: float, seed: int):
    """records -> DatasetDict(train/test)，列为 prompt / chosen / rejected（conversational 格式）。

    trl DPOTrainer 自动检测 conversational 格式并通过 apply_chat_template 渲染，
    无需在此手动渲染 prompt。
    切分按基问题分组（评审 #2）：数据生成侧每基问题产 8-10 个同义变体，行级随机
    切分会让变体同时落入 train/eval（实测 95.5% 泄漏），指标全面虚高。
    重依赖 datasets 在此函数内延迟导入。
    """
    from datasets import Dataset, DatasetDict  # 延迟导入：无 GPU 环境也可 import 本模块

    rows = [
        {
            "prompt": r["prompt"],
            "chosen": r["chosen"],
            "rejected": r["rejected"],
        }
        for r in records
    ]
    ds = Dataset.from_list(rows)
    if len(ds) < 2:
        raise ValueError(f"样本过少（{len(ds)} 条），无法划分训练/验证集")
    keys = [question_group_key(last_user_content(r["prompt"])) for r in records]
    test_size = max(1, round(len(ds) * eval_ratio))
    train_idx, test_idx = grouped_split_indices(keys, test_size=test_size, seed=seed)
    return DatasetDict({"train": ds.select(train_idx), "test": ds.select(test_idx)})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DPO 偏好对齐训练（trl DPOTrainer）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, help="dpo.jsonl 路径")
    parser.add_argument("--base_model", default="Qwen/Qwen2.5-7B-Instruct", help="基座模型（HF repo 或本地路径）")
    parser.add_argument("--sft_adapter", default=None,
                        help="可选：SFT 阶段产出的 LoRA adapter 路径；提供时先 merge 进基座再做 DPO")
    parser.add_argument("--output_dir", default="outputs/lora-dpo", help="DPO LoRA adapter 输出目录")
    parser.add_argument("--beta", type=float, default=0.1, help="DPO 温度系数（越小越偏离参考模型）")
    parser.add_argument("--lora_rank", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout")
    parser.add_argument("--lr", type=float, default=5e-5, help="学习率（DPO 通常小于 SFT）")
    parser.add_argument("--epochs", type=float, default=1, help="训练轮数（DPO 容易过拟合，1-2 轮为宜）")
    parser.add_argument("--batch_size", type=int, default=2, help="单卡 micro batch size（每条含 chosen+rejected 两个序列）")
    parser.add_argument("--grad_accum", type=int, default=16, help="梯度累积步数")
    parser.add_argument("--qlora", action="store_true", help="启用 4bit NF4 量化加载基座")
    parser.add_argument("--max_len", type=int, default=2048, help="prompt+completion 最大总长")
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
    from peft import LoraConfig, PeftModel, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import DPOConfig, DPOTrainer

    # ---- 可复现性 ----
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    # ---- 数据 ----
    records = load_dpo_jsonl(args.data)

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    split = build_hf_dataset(records, eval_ratio=args.eval_ratio, seed=args.seed)
    train_ds, eval_ds = split["train"], split["test"]
    logger.info("训练集 %d 条 / 验证集 %d 条", len(train_ds), len(eval_ds))

    # ---- 模型加载 ----
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
    model.config.use_cache = False

    # 在 SFT LoRA 之上继续训练：先合并 SFT adapter，作为 DPO 的起点模型
    if args.sft_adapter:
        logger.info("加载 SFT adapter 并合并进基座: %s", args.sft_adapter)
        model = PeftModel.from_pretrained(model, args.sft_adapter)
        model = model.merge_and_unload()

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

    # ---- DPO 训练参数 ----
    # 注意：trl 1.9.2 移除了 max_prompt_length，trl 自动处理 prompt 长度
    train_args = DPOConfig(
        output_dir=args.output_dir,
        beta=args.beta,
        max_length=args.max_len,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_steps=5,  # trl 1.9+ 移除了 warmup_ratio，改用绝对步数
        logging_steps=10,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        bf16=use_bf16,
        fp16=(not use_bf16) and torch.cuda.is_available(),
        gradient_checkpointing=not args.no_gradient_checkpointing,
        report_to=[],
        seed=args.seed,
    )

    # ---- MPS 缓存清理回调（仅 MPS 设备启用，工厂来自 finetune_utils）----
    mps_callbacks = []
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        mps_callbacks.append(make_mps_cache_cleanup_callback(every_n_steps=5))
        logger.info("已添加 MPS 缓存清理回调（每 5 步 torch.mps.empty_cache()）")

    trainer = DPOTrainer(
        model=model,
        ref_model=None,  # peft 模式下自动以"禁用 adapter 的模型"作为参考模型
        args=train_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
        peft_config=peft_config,
        callbacks=mps_callbacks,  # MPS 缓存清理（防止 Swap 恶性增长）
    )

    repro_info = {
        "base_model": args.base_model,
        "sft_adapter": args.sft_adapter,
        "beta": args.beta,
        "lora": {"rank": args.lora_rank, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
        "lr": args.lr,
        "epochs": args.epochs,
        "effective_batch_size": args.batch_size * args.grad_accum,
        "max_len": args.max_len,
        "gradient_checkpointing": not args.no_gradient_checkpointing,
        "seed": args.seed,
        "num_train_samples": len(train_ds),
        "num_eval_samples": len(eval_ds),
        "torch_version": torch.__version__,
    }
    logger.info("可复现信息:\n%s", json.dumps(repro_info, ensure_ascii=False, indent=2))

    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    logger.info("DPO LoRA adapter 与 tokenizer 已保存至 %s", args.output_dir)

    eval_metrics = trainer.evaluate()
    logger.info("最终验证集指标: %s", eval_metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
