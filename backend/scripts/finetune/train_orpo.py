#!/usr/bin/env python
"""ORPO（Odds Ratio Preference Optimization）训练脚本 — SFT+DPO 合一的单阶段对齐。

ORPO（Hong et al. 2024, ICML）在 SFT 损失中直接加 odds ratio 偏好项，一步完成对齐：
    L_ORPO = L_SFT(y_w) + λ · L_OR(y_w, y_l)
    L_OR  = -log σ( log(odds(y_w)) - log(odds(y_l)) )
    odds(y) = P(y|x) / (1 - P(y|x))

对比 SFT→DPO 两阶段：
    - 两阶段：先 SFT（学话术/格式）→ 再 DPO（学偏好），需两次全量训练 + 中间 SFT adapter
    - ORPO 单阶段：SFT loss 保话术 + odds ratio 学偏好，一步到位，省一个阶段、省一次训练

优势（见微调.md P3-18 / todo.md）：
    1. 单阶段 → 训练总时长减半（省 SFT 阶段）
    2. 无 ref_model → 省一份前向显存（同 SimPO，7B ~18GB）
    3. SFT loss 约束 → 不偏离基座太远，比纯 DPO 更稳

实现说明：trl 1.9.2 的 ORPO 在 ``trl.experimental.orpo`` 下（API 不稳定但可用）。
ORPOTrainer 数据要求 prompt/chosen/rejected 为 **str**（非 conversational list），
本脚本在加载时用 apply_chat_template 将 conversational dpo.jsonl 转为 str 格式。

依赖：同 train_dpo.py（trl>=1.9 提供 experimental.orpo）

用法：
    # 1.5B 单阶段对齐（从基座开始，无需 SFT 前置）
    python scripts/finetune/train_orpo.py \\
        --data data/open/dpo.jsonl --base_model models/Qwen2.5-1.5B-Instruct \\
        --output_dir outputs/orpo-v1-1.5b \\
        --beta 0.1 --lr 1e-5 --epochs 1

    # 7B 单阶段（省 SFT + 省 ref 显存）
    python scripts/finetune/train_orpo.py \\
        --data data/open/dpo.jsonl --base_model models/Qwen2.5-7B-Instruct \\
        --output_dir outputs/orpo-v1-7b \\
        --beta 0.1 --lr 1e-5 --epochs 1
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys

# 复用 train_dpo 的数据加载 / bf16 检测（避免重复代码）
from train_dpo import DEFAULT_TARGET_MODULES, _detect_bf16, load_dpo_jsonl
# 分组切分与 MPS 缓存清理回调（评审 #2/#10）
from finetune_utils import (
    grouped_split_indices,
    last_user_content,
    make_mps_cache_cleanup_callback,
    question_group_key,
)

logger = logging.getLogger("train_orpo")


def convert_to_orpo_format(records: list[dict], tokenizer) -> list[dict]:
    """conversational dpo.jsonl → ORPO str 格式（prompt/chosen/rejected 为 str）。

    ORPOTrainer 要求 prompt/chosen/rejected 为 str（非 conversational list）：
      - prompt：用 apply_chat_template(add_generation_prompt=True) 渲染到 assistant 开头
      - chosen/rejected：取 assistant 消息的 content 纯文本

    ORPOTrainer 内部 build_tokenized_answer 会把 prompt + chosen/rejected 拼接成完整序列。
    """
    rows: list[dict] = []
    for r in records:
        prompt_str = tokenizer.apply_chat_template(
            r["prompt"], add_generation_prompt=True, tokenize=False)
        chosen_str = r["chosen"][-1]["content"]
        rejected_str = r["rejected"][-1]["content"]
        rows.append({"prompt": prompt_str, "chosen": chosen_str, "rejected": rejected_str})
    logger.info("转换 conversational → ORPO str 格式：%d 条", len(rows))
    return rows


def build_orpo_dataset(rows: list[dict], keys: list[str], eval_ratio: float, seed: int):
    """rows → DatasetDict(train/test)，列为 prompt/chosen/rejected（str 格式）。

    keys 为每条样本的基问题分组键（由原始 conversational prompt 计算）：
    切分按组整体进行（评审 #2），防同义变体跨 train/eval 泄漏。
    """
    from datasets import Dataset, DatasetDict

    ds = Dataset.from_list(rows)
    if len(ds) < 2:
        raise ValueError(f"样本过少（{len(ds)} 条），无法划分训练/验证集")
    test_size = max(1, round(len(ds) * eval_ratio))
    train_idx, test_idx = grouped_split_indices(keys, test_size=test_size, seed=seed)
    return DatasetDict({"train": ds.select(train_idx), "test": ds.select(test_idx)})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ORPO 单阶段对齐训练（SFT+DPO 合一，trl experimental.orpo）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, help="dpo.jsonl 路径（conversational 格式，自动转 str）")
    parser.add_argument("--base_model", default="Qwen/Qwen2.5-7B-Instruct", help="基座模型")
    parser.add_argument("--sft_adapter", default=None,
                        help="可选：SFT adapter（ORPO 通常从基座开始，无需 SFT 前置；"
                             "提供时先 merge 进基座再做 ORPO）")
    parser.add_argument("--output_dir", default="outputs/lora-orpo", help="ORPO adapter 输出目录")
    # ORPO 特有参数
    parser.add_argument("--beta", type=float, default=0.1,
                        help="ORPO λ：odds ratio loss 权重（论文默认 0.1，过大易过拟合偏好）")
    parser.add_argument("--max_len", type=int, default=2048, help="prompt+completion 最大总长")
    # LoRA / 训练参数（与 train_dpo 对齐）
    parser.add_argument("--lora_rank", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout")
    parser.add_argument("--lr", type=float, default=1e-5,
                        help="学习率（ORPO 含 SFT loss 较稳，LoRA 用 1e-5；"
                             "ORPOConfig 默认 1e-6 适合全参数，LoRA 需放大）")
    parser.add_argument("--epochs", type=float, default=1, help="训练轮数")
    parser.add_argument("--batch_size", type=int, default=2, help="单卡 micro batch size")
    parser.add_argument("--grad_accum", type=int, default=16, help="梯度累积步数")
    parser.add_argument("--eval_ratio", type=float, default=0.05, help="验证集比例")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--no_gradient_checkpointing", action="store_true",
                        help="关闭梯度检查点（1.5B 推荐；7B 勿用会 OOM）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 静默 trl experimental 警告
    import os
    os.environ.setdefault("TRL_EXPERIMENTAL_SILENCE", "1")

    import torch
    from peft import LoraConfig, PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl.experimental.orpo import ORPOConfig, ORPOTrainer

    # ---- 可复现性 ----
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    # ---- 数据：conversational → ORPO str 格式（分组键在转换前从原始 prompt 计算）----
    records = load_dpo_jsonl(args.data)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    group_keys = [question_group_key(last_user_content(r["prompt"])) for r in records]
    orpo_rows = convert_to_orpo_format(records, tokenizer)
    split = build_orpo_dataset(orpo_rows, group_keys, eval_ratio=args.eval_ratio, seed=args.seed)
    train_ds, eval_ds = split["train"], split["test"]
    logger.info("训练集 %d 条 / 验证集 %d 条", len(train_ds), len(eval_ds))

    # ---- 模型加载（与 train_dpo 一致）----
    use_bf16 = _detect_bf16()
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.bfloat16 if use_bf16 else torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False

    if args.sft_adapter:
        logger.info("加载 SFT adapter 并合并进基座: %s", args.sft_adapter)
        model = PeftModel.from_pretrained(model, args.sft_adapter)
        model = model.merge_and_unload()

    peft_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=DEFAULT_TARGET_MODULES,
    )

    # ---- ORPO 训练参数 ----
    # ORPOConfig 继承 _BaseConfig（含 TrainingArguments 全部字段 + ORPO 特有参数）
    # 注：ORPOConfig 默认 gradient_checkpointing=True、disable_dropout=True、lr=1e-6
    train_args = ORPOConfig(
        output_dir=args.output_dir,
        beta=args.beta,                    # λ：odds ratio loss 权重
        max_length=args.max_len,
        disable_dropout=True,              # ORPO 论文建议禁用 dropout 稳定训练
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.1,
        logging_steps=10,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        bf16=use_bf16,
        fp16=(not use_bf16) and torch.cuda.is_available(),
        gradient_checkpointing=not args.no_gradient_checkpointing,
        report_to=[],
        seed=args.seed,
        trust_remote_code=True,
    )

    # ---- MPS 缓存清理回调（评审 #10：ORPO 此前缺失，与 DPO/SimPO 对齐）----
    mps_callbacks = []
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        mps_callbacks.append(make_mps_cache_cleanup_callback(every_n_steps=5))
        logger.info("已添加 MPS 缓存清理回调（每 5 步 torch.mps.empty_cache()）")

    # ---- ORPOTrainer：单阶段 SFT+偏好，无 ref_model ----
    trainer = ORPOTrainer(
        model=model,
        args=train_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
        peft_config=peft_config,
        callbacks=mps_callbacks,
    )

    repro_info = {
        "algo": "orpo",
        "base_model": args.base_model,
        "sft_adapter": args.sft_adapter,
        "beta_lambda": args.beta,
        "lora": {"rank": args.lora_rank, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
        "lr": args.lr,
        "epochs": args.epochs,
        "effective_batch_size": args.batch_size * args.grad_accum,
        "max_len": args.max_len,
        "gradient_checkpointing": not args.no_gradient_checkpointing,
        "disable_dropout": True,
        "seed": args.seed,
        "num_train_samples": len(train_ds),
        "num_eval_samples": len(eval_ds),
        "note": "trl 1.9.2 experimental.orpo（API 不稳定）；单阶段 SFT+odds ratio，无 ref_model",
        "torch_version": torch.__version__,
    }
    logger.info("可复现信息:\n%s", json.dumps(repro_info, ensure_ascii=False, indent=2))

    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    logger.info("ORPO LoRA adapter 与 tokenizer 已保存至 %s", args.output_dir)

    eval_metrics = trainer.evaluate()
    logger.info("最终验证集指标: %s", eval_metrics)
    logger.info("下一步: 用 eval_boundary_200.py 评测拒答率，对比 SFT+DPO 两阶段（见微调.md P3-18）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
