#!/usr/bin/env python
"""GRPO 在线强化训练脚本 — 在 SFT/基座之上做 on-policy 偏好优化（trl GRPOTrainer）。

与 DPO 的核心区别：
    - DPO 是离线的（数据预先给 chosen/rejected），不需要生成
    - GRPO 是在线的：每个 prompt 采样 G 个回复（group），用 rule-based reward
      打分，组内标准化（r - mean) / std 作为优势 A_i，PPO-clip 更新 policy
    - 不需要 critic 网络（比 PPO 省一份模型），ref_model 在 peft 模式下自动用
      "禁用 adapter 的基座"（与 DPO 一致，0 额外权重）

输入数据（复用 dpo.jsonl 的 prompt，conversational 格式）：
    {"prompt":[{"role":"system","content":"..."},{"role":"user","content":"..."}],
     "chosen":[...], "rejected":[...]}   ← GRPO 只用 prompt，chosen/rejected 忽略

reward 设计（rule-based，针对企业知识库拒答目标）：
    - 边界问题（写诗/股票/机票/天气/八字/医疗/法律/原神/星座 等 16 类关键词）：
        含拒答信号 → +1.0；硬答 → -1.0
    - 工作问题（非边界）：实质回答（长度≥20 且非拒答）→ +1.0；误拒答 → -1.0
    局限：rule-based 易 reward hacking（模型堆"建议/无法"骗分），冒烟验证链路够用；
         生产级需换 LLM-as-judge reward model（见微调.md GRPO 章节后续方向）。

依赖（同 train_dpo.py，trl>=1.9 提供 GRPOTrainer）：
    pip install "torch>=2.2" "transformers>=4.45" "peft>=0.13" "trl>=1.9" \
                "datasets>=2.20" "accelerate>=0.34"

运行示例（1.5B 冒烟，验证 MPS 兼容 + reward 趋势）：
    python scripts/finetune/train_grpo.py \
        --data data/open/dpo.jsonl --base_model models/Qwen2.5-1.5B-Instruct \
        --output_dir outputs/grpo-v1-1.5b-smoke \
        --num_generations 4 --max_prompt_length 256 --max_completion_length 128 \
        --beta 0.04 --lr 1e-6 --temperature 1.0 --max_steps 10
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("train_grpo")

#: LoRA 注入目标模块（与 train_lora.py / train_dpo.py 保持一致）
DEFAULT_TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]

#: 边界问题触发词（对齐 generate_dpo_data.py 的 16 类 _BOUNDARY_CASES_EXPANDED）
#: 命中任一即判定为边界问题，reward 期望拒答
BOUNDARY_KEYWORDS: tuple[str, ...] = (
    "写诗", "写一首", "股票", "行情", "机票", "预订", "出差",
    "天气", "翻译", "写代码", "写一段代码", "餐厅", "好吃",
    "八字", "命理", "算命", "星座", "头痛", "生病", "怎么办",
    "裁员", "告", "劳动", "论文", "学术", "抑郁", "心情",
    "原神", "游戏", "比价", "值得买", "红烧肉", "菜谱", "怎么做",
)

#: 拒答信号词（纯"不回答"语义，不含引导）。命中即视为拒答
REFUSAL_SIGNALS: tuple[str, ...] = (
    "不在服务范围", "不在我的服务", "不在知识库", "不提供",
    "无法提供", "无法查询", "无法访问",
    "我是企业知识库助手", "欢迎提问", "欢迎随时提问",
)

#: 引导信号词（拒答后给出替代方案/联系入口）。拒答+引导 > 干拒答
GUIDANCE_SIGNALS: tuple[str, ...] = (
    "建议您使用", "建议使用", "建议咨询", "请咨询",
    "请联系", "可联系", "请使用", "可拨打", "拨打",
)

#: 企业系统入口关键词（工作问题回答含这些=更高质量，对齐 generate_dpo_data 的企业系统名）
ENTERPRISE_SYSTEMS: tuple[str, ...] = (
    "example.com", "企业邮箱", "企业统一身份", "IT 服务管理", "IT服务管理",
    "差旅平台", "内网软件", "考勤系统", "费用报销", "报销系统",
    "文件服务器", "OA 系统", "OA系统", "IT 服务台", "IT服务台", "HR 部门", "HR部门",
    "idp", "itsm", "tripmgmt",
)

#: 冒烟判定"实质回答"的最小长度（字符），低于此视为空话
SUBSTANTIVE_MIN_LEN = 20


def iter_jsonl(path: str | Path) -> Iterator[tuple[int, dict]]:
    """逐行读取 JSONL，跳过坏行。Yields (行号, dict)。"""
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
                continue
            yield lineno, obj


def _validate_messages(messages: Any) -> bool:
    """校验消息列表：非空 list，每条含 str 类型的 role/content。"""
    if not isinstance(messages, list) or len(messages) == 0:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            return False
        if not isinstance(msg.get("role"), str) or not isinstance(msg.get("content"), str):
            return False
        if not msg["content"].strip():
            return False
    return True


def load_grpo_prompts(path: str | Path, limit: int | None = None) -> list[list[dict]]:
    """加载 dpo.jsonl，仅取 prompt（conversational），忽略 chosen/rejected。

    GRPO 在线生成回复，不需要预先偏好对。limit 用于冒烟截取少量样本。
    """
    records: list[list[dict]] = []
    for lineno, obj in iter_jsonl(path):
        prompt = obj.get("prompt")
        if not _validate_messages(prompt):
            logger.warning("跳过样本 %s:%d — prompt 非合法消息列表", path, lineno)
            continue
        records.append(prompt)
        if limit and len(records) >= limit:
            break
    logger.info("加载 GRPO prompt %d 条 — %s", len(records), path)
    return records


def _extract_user_text(prompt: Any) -> str:
    """从 conversational prompt 提取最后一条 user content（reward 判别用）。"""
    if isinstance(prompt, list):
        for msg in reversed(prompt):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return msg.get("content", "")
    return str(prompt)


def _completion_text(completion: Any) -> str:
    """completion 可能是 str（trl 默认）或 list[{role,content}]（防御）。"""
    if isinstance(completion, str):
        return completion
    if isinstance(completion, list):
        for msg in reversed(completion):
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
    return str(completion)


def enterprise_boundary_reward(prompts: list, completions: list, **kwargs) -> list[float]:
    """连续 reward（v2）：多梯度分值减少组内 std=0，让 GRPO 有持续学习信号。

    边界问题：拒答+引导=1.0 / 干拒答=0.5 / 硬答=-1.0
    工作问题：实质+企业入口=1.0 / 实质无入口=0.6 / 短空话=0.1 / 误拒答=-1.0

    v1 二值 reward（±1）导致组内 4 个回复趋同 std=0、梯度为 0（见微调.md 9.5）；
    v2 用 5 档梯度让"质量相近但有差异"的回复也能产生优势信号。

    签名对齐 trl GRPOTrainer：``(prompts, completions, **kwargs) -> list[float]``。
    """
    rewards: list[float] = []
    for prompt, completion in zip(prompts, completions):
        user_text = _extract_user_text(prompt)
        comp_text = _completion_text(completion)
        is_boundary = any(kw in user_text for kw in BOUNDARY_KEYWORDS)
        has_refusal = any(sig in comp_text for sig in REFUSAL_SIGNALS)
        has_guidance = any(sig in comp_text for sig in GUIDANCE_SIGNALS)
        has_enterprise = any(s in comp_text for s in ENTERPRISE_SYSTEMS)
        is_substantive = len(comp_text) >= SUBSTANTIVE_MIN_LEN

        if is_boundary:
            if has_refusal and has_guidance:
                r = 1.0    # 拒答+引导（最佳）
            elif has_refusal:
                r = 0.5    # 干拒答，无替代方案
            else:
                r = -1.0   # 硬答（最差）
        else:
            if has_refusal:
                r = -1.0   # 工作问题误拒答
            elif has_enterprise and is_substantive:
                r = 1.0    # 实质+企业入口（最佳）
            elif is_substantive:
                r = 0.6    # 实质但无企业入口
            else:
                r = 0.1    # 短/空话
        rewards.append(r)
    return rewards


def build_hf_dataset(prompts: list[list[dict]], eval_ratio: float, seed: int):
    """prompts -> DatasetDict(train/test)，列名 ``prompt``（conversational）。

    trl GRPOTrainer 要求 dataset 含 "prompt" 列。重依赖 datasets 延迟导入。
    """
    from datasets import Dataset

    ds = Dataset.from_list([{"prompt": p} for p in prompts])
    if len(ds) < 2:
        raise ValueError(f"样本过少（{len(ds)} 条），无法划分训练/验证集")
    test_size = max(1, round(len(ds) * eval_ratio))
    return ds.train_test_split(test_size=test_size, seed=seed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GRPO 在线强化训练（trl GRPOTrainer，rule-based reward）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, help="dpo.jsonl 路径（仅取 prompt 列）")
    parser.add_argument("--base_model", default="Qwen/Qwen2.5-1.5B-Instruct",
                        help="基座模型（HF repo 或本地路径）")
    parser.add_argument("--sft_adapter", default=None,
                        help="可选：SFT LoRA adapter 路径；提供时先 merge 进基座再做 GRPO")
    parser.add_argument("--output_dir", default="outputs/grpo-v1", help="GRPO LoRA adapter 输出目录")
    # GRPO 生成参数
    parser.add_argument("--num_generations", type=int, default=4,
                        help="每个 prompt 采样 G 个回复（组大小，冒烟用 4 省时）")
    parser.add_argument("--max_prompt_length", type=int, default=256, help="prompt 最大长度（超出左截断）")
    parser.add_argument("--max_completion_length", type=int, default=128, help="生成回复最大长度")
    parser.add_argument("--generation_batch_size", type=int, default=4,
                        help="一次生成的 prompt 数（×num_generations=总生成数）")
    parser.add_argument("--temperature", type=float, default=1.0, help="生成温度（组内需多样性，默认1.0）")
    parser.add_argument("--beta", type=float, default=0.04, help="KL 系数（0=无约束，0.04=轻约束防偏离）")
    # LoRA
    parser.add_argument("--lora_rank", type=int, default=8, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=16, help="LoRA alpha")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout")
    # 训练
    parser.add_argument("--lr", type=float, default=1e-6, help="学习率（GRPO 对 lr 敏感，远小于 SFT）")
    parser.add_argument("--epochs", type=float, default=1, help="训练轮数（冒烟用 max_steps 限制）")
    parser.add_argument("--max_steps", type=int, default=-1, help="最大步数（>0 时覆盖 epochs，冒烟用 10）")
    parser.add_argument("--grad_accum", type=int, default=1, help="梯度累积步数")
    parser.add_argument("--limit", type=int, default=None, help="冒烟：只取前 N 条 prompt")
    parser.add_argument("--eval_ratio", type=float, default=0.1, help="验证集比例")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--no_gradient_checkpointing", action="store_true",
                        help="关闭梯度检查点（1.5B 可关提速；见微调.md 附录D）")
    return parser.parse_args(argv)


def _detect_bf16() -> bool:
    """检测训练设备是否支持 bf16（CUDA 或 Apple Silicon MPS）。"""
    import torch
    if torch.cuda.is_available():
        return bool(torch.cuda.is_bf16_supported())
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        try:
            torch.zeros(1, dtype=torch.bfloat16, device="mps")
            return True
        except Exception:
            return False
    return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import torch
    from peft import LoraConfig, PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import GRPOConfig, GRPOTrainer

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    # ---- 数据（仅 prompt）----
    prompts = load_grpo_prompts(args.data, limit=args.limit)
    split = build_hf_dataset(prompts, eval_ratio=args.eval_ratio, seed=args.seed)
    train_ds, eval_ds = split["train"], split["test"]
    logger.info("训练集 %d 条 / 验证集 %d 条", len(train_ds), len(eval_ds))

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # ---- 模型 ----
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

    # ---- GRPO 训练参数 ----
    train_args = GRPOConfig(
        output_dir=args.output_dir,
        num_generations=args.num_generations,
        # 注：trl 1.9.2 的 GRPOConfig 无 max_prompt_length（自动按模型 max_len 处理）
        max_completion_length=args.max_completion_length,
        generation_batch_size=args.generation_batch_size,
        temperature=args.temperature,
        beta=args.beta,
        learning_rate=args.lr,
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.generation_batch_size,
        gradient_accumulation_steps=args.grad_accum,
        gradient_checkpointing=not args.no_gradient_checkpointing,
        bf16=use_bf16,
        fp16=(not use_bf16) and torch.cuda.is_available(),
        logging_steps=1,
        eval_strategy="no",
        save_strategy="no",
        report_to=[],
        seed=args.seed,
    )

    trainer = GRPOTrainer(
        model=model,
        reward_funcs=enterprise_boundary_reward,
        args=train_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
        peft_config=peft_config,
    )

    repro_info = {
        "base_model": args.base_model,
        "sft_adapter": args.sft_adapter,
        "method": "GRPO",
        "reward": "rule-based (boundary refusal + work substantive)",
        "num_generations": args.num_generations,
        "max_prompt_length": args.max_prompt_length,
        "max_completion_length": args.max_completion_length,
        "beta": args.beta,
        "temperature": args.temperature,
        "lora": {"rank": args.lora_rank, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
        "lr": args.lr,
        "max_steps": args.max_steps,
        "epochs": args.epochs,
        "num_train_samples": len(train_ds),
        "gradient_checkpointing": not args.no_gradient_checkpointing,
        "seed": args.seed,
        "torch_version": torch.__version__,
    }
    logger.info("可复现信息:\n%s", json.dumps(repro_info, ensure_ascii=False, indent=2))

    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    logger.info("GRPO LoRA adapter 与 tokenizer 已保存至 %s", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
