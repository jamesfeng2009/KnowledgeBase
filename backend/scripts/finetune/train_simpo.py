#!/usr/bin/env python
"""SimPO（Simple Preference Optimization）训练脚本 — 无参考模型的 DPO 变体。

SimPO（Meng et al. 2024, ICLR）去掉 DPO 的 ref_model，损失改用长度归一化似然比 + gamma margin：
    L_SimPO = -log σ( β·(logπ(y_w|x)/|y_w| - logπ(y_l|x)/|y_l|) - γ )
对比 DPO：
    L_DPO   = -log σ( β·(logπ(y_w|x) - logπ_ref(y_w|x)) - β·(logπ(y_l|x) - logπ_ref(y_l|x)) )

SimPO 优势（见微调.md P3-17 / todo.md）：
    1. 无 ref_model → 省一份前向显存（7B 从 21-23GB 降到 ~18GB，M3 Max 36G 更稳）
    2. 长度归一化（÷|y|）→ 消除"长回复天然 logp 之和更低"的偏差
    3. gamma margin → 增大 chosen/rejected 分离度，训练信号更明确

参数差异（vs DPO）：
    - beta 更大（DPO 0.1 → SimPO 2.0~2.5）：长度归一化后 logp 差值缩小，需放大 beta 补偿
    - simpo_gamma（margin，0.5~1.4）：chosen/rejected 分数差需超过 γ 才算"学到了"

实现说明：trl 1.9.2 无原生 CPOTrainer/SimPOTrainer，本脚本继承 DPOTrainer 重写
``_compute_loss`` 实现 SimPO（跳过 ref 前向 + 长度归一化 + gamma margin）。trl>=0.18
有原生 CPOTrainer(loss_type="simpo")，可后续升级替换为原生实现（见微调.md P3-17 待办）。

依赖：同 train_dpo.py（trl>=1.9 提供基类 DPOTrainer）

用法：
    # 1.5B 验证（先跑通链路）
    python scripts/finetune/train_simpo.py \\
        --data data/open/dpo.jsonl --base_model models/Qwen2.5-1.5B-Instruct \\
        --sft_adapter outputs/sft-v3-distill --output_dir outputs/simpo-v1-1.5b \\
        --beta 2.0 --simpo_gamma 1.0 --lr 5e-5 --epochs 1

    # 7B（省显存，M3 Max 更稳）
    python scripts/finetune/train_simpo.py \\
        --data data/open/dpo.jsonl --base_model models/Qwen2.5-7B-Instruct \\
        --sft_adapter outputs/sft-v5-7b-transformers --output_dir outputs/simpo-v1-7b \\
        --beta 2.0 --simpo_gamma 1.0 --lr 5e-5 --epochs 1
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys

# 复用 train_dpo 的数据加载 / 模型加载 / bf16 检测（避免重复代码）
from train_dpo import (
    DEFAULT_TARGET_MODULES,
    _detect_bf16,
    build_hf_dataset,
    load_dpo_jsonl,
)

logger = logging.getLogger("train_simpo")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SimPO 偏好对齐训练（无 ref_model，继承 DPOTrainer 重写 loss）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, help="dpo.jsonl 路径")
    parser.add_argument("--base_model", default="Qwen/Qwen2.5-7B-Instruct", help="基座模型")
    parser.add_argument("--sft_adapter", default=None,
                        help="可选：SFT LoRA adapter 路径；提供时先 merge 进基座再做 SimPO")
    parser.add_argument("--output_dir", default="outputs/lora-simpo", help="SimPO adapter 输出目录")
    # SimPO 特有参数
    parser.add_argument("--beta", type=float, default=2.0,
                        help="SimPO 温度系数（比 DPO 大：长度归一化后 logp 差值缩小，需放大补偿。"
                             "论文推荐 2.0~2.5，DPO 常用 0.1）")
    parser.add_argument("--simpo_gamma", type=float, default=1.0,
                        help="SimPO margin（chosen/rejected 分数差需超过 γ。论文推荐 0.5~1.4）")
    # LoRA / 训练参数（与 train_dpo 对齐）
    parser.add_argument("--lora_rank", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout")
    parser.add_argument("--lr", type=float, default=5e-5, help="学习率")
    parser.add_argument("--epochs", type=float, default=1, help="训练轮数")
    parser.add_argument("--batch_size", type=int, default=2, help="单卡 micro batch size")
    parser.add_argument("--grad_accum", type=int, default=16, help="梯度累积步数")
    parser.add_argument("--max_len", type=int, default=2048, help="prompt+completion 最大总长")
    parser.add_argument("--eval_ratio", type=float, default=0.05, help="验证集比例")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--no_gradient_checkpointing", action="store_true",
                        help="关闭梯度检查点（1.5B 推荐；7B 勿用会 OOM）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import torch
    from peft import LoraConfig, PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
    from trl import DPOConfig, DPOTrainer
    from trl.trainer.utils import selective_log_softmax
    import torch.nn.functional as F

    # ---- MPS 缓存清理回调：防止 Metal buffer 碎片化累积导致 swap 增长 ----
    # PyTorch MPS 后端的 Metal buffer 不像 CUDA 积极回收临时张量内存。DPO/SimPO
    # 每步产生 ~4.7GB 临时 logits（2×768×152064词表×2字节），Python 侧释放后
    # GPU 侧仍缓存。18 步内可接受，42+ 步时碎片化导致系统用 swap 兜底→恶性循环。
    # 每 5 步调 torch.mps.empty_cache() 强制回收，阻断碎片化累积。
    class MPSCacheCleanupCallback(TrainerCallback):
        """每隔 N 步清空 MPS Metal 缓存，防止碎片化累积。"""

        def __init__(self, every_n_steps: int = 5):
            self.every_n_steps = every_n_steps

        def on_step_end(self, args, state, control, **kwargs):
            if state.global_step % self.every_n_steps == 0:
                torch.mps.empty_cache()

    # ---- SimPOTrainer：继承 DPOTrainer，重写 _compute_loss 实现 SimPO ----
    class SimPOTrainer(DPOTrainer):
        """SimPO trainer — 无 ref_model + 长度归一化似然比 + gamma margin。

        重写 ``_compute_loss``：跳过 ref 前向（省一份显存），对 policy logps 做
        长度归一化（÷completion token 数），加 gamma margin。reward 定义对齐 SimPO
        论文：r = β·logπ(y|x)/|y| - γ/2。
        """

        def __init__(self, *a, simpo_gamma: float = 1.0, **kw):
            super().__init__(*a, **kw)
            self.simpo_gamma = simpo_gamma

        def _compute_loss(self, model, inputs, return_outputs=False):
            mode = "train" if self.model.training else "eval"

            # ---- 1. policy 前向（复用 DPOTrainer 的 logps 计算逻辑，但不计算 ref）----
            _non_model_keys = {"completion_mask", "ref_chosen_logps", "ref_rejected_logps"}
            model_kwargs = {k: v for k, v in inputs.items() if k not in _non_model_keys}
            model_kwargs["use_cache"] = False
            outputs = model(**model_kwargs)

            input_ids = inputs["input_ids"]
            completion_mask = inputs["completion_mask"]
            shift_logits = outputs.logits[..., :-1, :]
            shift_labels = input_ids[..., 1:]
            shift_completion_mask = completion_mask[..., 1:]

            per_token_logps = selective_log_softmax(shift_logits, shift_labels)
            per_token_logps[shift_completion_mask == 0] = 0.0  # mask 非 completion token
            logps = per_token_logps.sum(dim=1)  # 序列 logprob 之和
            chosen_logps, rejected_logps = logps.chunk(2, dim=0)  # batch=[chosen, rejected]

            # ---- 2. SimPO 核心：长度归一化 + gamma margin（无 ref 前向）----
            comp_lens = shift_completion_mask.sum(dim=1).clamp(min=1)  # completion token 数
            chosen_lens, rejected_lens = comp_lens.chunk(2, dim=0)
            # 长度归一化（÷|y|）—— SimPO 消除长回复天然 logp 更低的偏差
            norm_chosen = chosen_logps / chosen_lens
            norm_rejected = rejected_logps / rejected_lens
            # SimPO loss = -log σ( β·(norm_chosen - norm_rejected) - γ )
            logits = self.beta * (norm_chosen - norm_rejected) - self.simpo_gamma
            loss = -F.logsigmoid(logits).mean()

            # ---- 3. 记录 metrics（与 DPO 对齐，便于对比训练日志）----
            # SimPO reward = β·logπ(y|x)/|y| - γ/2（论文定义，用于监控 chosen>rejected）
            chosen_rewards = self.beta * norm_chosen - self.simpo_gamma / 2
            rejected_rewards = self.beta * norm_rejected - self.simpo_gamma / 2

            agg_chosen = self.accelerator.gather(chosen_rewards)
            agg_rejected = self.accelerator.gather(rejected_rewards)
            self._metrics[mode]["rewards/chosen"].append(agg_chosen.mean().item())
            self._metrics[mode]["rewards/rejected"].append(agg_rejected.mean().item())
            acc = (chosen_rewards > rejected_rewards).float()
            self._metrics[mode]["rewards/accuracies"].append(
                self.accelerator.gather(acc).mean().item())
            self._metrics[mode]["rewards/margins"].append(
                self.accelerator.gather(chosen_rewards - rejected_rewards).mean().item())
            self._metrics[mode]["logps/chosen"].append(
                self.accelerator.gather(chosen_logps).mean().item())
            self._metrics[mode]["logps/rejected"].append(
                self.accelerator.gather(rejected_logps).mean().item())

            return loss

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

    # ---- SimPO 训练参数（基于 DPOConfig，loss_type 无关——loss 在 _compute_loss 重写）----
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
    )

    # ---- SimPOTrainer：ref_model=None，不传 ref（SimPO 无 ref）----
    # 注：DPOTrainer peft 模式 ref_model=None 时会用 disable_adapter 做 ref，
    # 但我们重写了 _compute_loss 跳过 ref 前向，所以 ref 逻辑不触发——真正省一份显存。
    mps_callbacks = []
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        mps_callbacks.append(MPSCacheCleanupCallback(every_n_steps=5))
        logger.info("已添加 MPS 缓存清理回调（每 5 步 torch.mps.empty_cache()）")

    trainer = SimPOTrainer(
        model=model,
        ref_model=None,  # SimPO 无 ref（_compute_loss 重写跳过 ref 前向）
        args=train_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
        peft_config=peft_config,
        simpo_gamma=args.simpo_gamma,
        callbacks=mps_callbacks,
    )

    repro_info = {
        "algo": "simpo",
        "base_model": args.base_model,
        "sft_adapter": args.sft_adapter,
        "beta": args.beta,
        "simpo_gamma": args.simpo_gamma,
        "lora": {"rank": args.lora_rank, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
        "lr": args.lr,
        "epochs": args.epochs,
        "effective_batch_size": args.batch_size * args.grad_accum,
        "max_len": args.max_len,
        "gradient_checkpointing": not args.no_gradient_checkpointing,
        "seed": args.seed,
        "num_train_samples": len(train_ds),
        "num_eval_samples": len(eval_ds),
        "note": "trl 1.9.2 无原生 CPOTrainer，继承 DPOTrainer 重写 _compute_loss",
        "torch_version": torch.__version__,
    }
    logger.info("可复现信息:\n%s", json.dumps(repro_info, ensure_ascii=False, indent=2))

    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    logger.info("SimPO LoRA adapter 与 tokenizer 已保存至 %s", args.output_dir)

    eval_metrics = trainer.evaluate()
    logger.info("最终验证集指标: %s", eval_metrics)
    logger.info("下一步: 用 eval_boundary_200.py 评测拒答率，对比 DPO（见微调.md P3-17）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
