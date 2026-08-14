#!/usr/bin/env python
"""Constitutional AI 改写扩 DPO 数据 — RLAIF 路线 C（CAI）。

与 generate_rlaif_data.py（路线 A，pairwise 选好坏）的区别：
    - 路线 A：采样 2 候选 → 裁判选 A/B/TIE → 偏好对 (chosen, rejected) 从采样里挑
    - 路线 C：采样 1 回复 → 若是边界硬答 → critic 按宪法批评+改写出"拒答+引导"版
              → 偏好对 (chosen=改写版, rejected=原硬答)

CAI 优势（见微调.md 第十二章）：chosen 是 AI 主动改写的"理想回复"，比从两个
采样里挑更精细；对应 Anthropic Constitutional AI 的核心思想（AI 自我批评+修订）。
局限：依赖 critic 7B 的改写能力，改写质量不如 Claude；DPO 对 20-30% 标注噪声
有鲁棒性，可容忍部分次优改写。

流程：
    1. 边界类 prompt 池（CAI 主要针对边界硬答，工作问题无需改写）
    2. policy 采样 1 个回复（temperature 0.7 造自然硬答）
    3. 规则判断是否"硬答"（边界问题 + 无拒答信号 = 硬答）→ 不是硬答则跳过
    4. critic 模型按 CAI 宪法 system prompt 改写出"拒答+引导"版
    5. 偏好对 = {prompt, chosen: 改写版, rejected: 原硬答}

断点续传：复用 generate_rlaif_data 的 _prompt_hash/load_existing/append_pair。

依赖：
    pip install "torch>=2.2" "transformers>=4.45" "peft>=0.13"

用法：
    # 冒烟（30 对，验证改写流程连通）
    python scripts/finetune/generate_cai_data.py \\
        --output data/open/dpo_cai.jsonl --count 30 \\
        --policy_model models/Qwen2.5-1.5B-Instruct \\
        --sft_adapter outputs/sft-v3-distill \\
        --critic_model models/Qwen2.5-7B-Instruct

    # 正式（300 对，AutoDL A100 40G）
    python scripts/finetune/generate_cai_data.py \\
        --output data/open/dpo_cai.jsonl --count 300 \\
        --policy_model models/Qwen2.5-1.5B-Instruct \\
        --sft_adapter outputs/sft-v3-distill \\
        --critic_model models/Qwen2.5-7B-Instruct --resume
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from typing import Any

# 复用 generate_dpo_data 的弱 prompt（SYSTEM_PROMPT）与边界 prompt 构造
from generate_dpo_data import SYSTEM_PROMPT, generate_boundary_pairs
# 复用 generate_rlaif_data 的基础设施：模型加载/设备检测/断点续传/边界变体
from generate_rlaif_data import (
    _detect_bf16,
    _prompt_hash,
    append_pair,
    build_extra_boundary_prompts,
    load_existing,
    load_policy_model,
)
# 边界/拒答信号词统一来源（评审 #1/#9 收敛到 finetune_utils）
from finetune_utils import (
    BOUNDARY_KEYWORDS,
    REFUSAL_KEYWORDS,
    extract_question_text,
    last_user_content,
)

logger = logging.getLogger("generate_cai")


# ============================================================================
# CAI 宪法 system prompt（critique-rewrite）
# ============================================================================
#: 设计要点：① 明确边界定义 ② 只在硬答时改写 ③ 已拒答则原样返回 ④ 只输出最终回复
CAI_REWRITE_SYS = (
    "你是企业知识库助手的合规审阅官（Constitutional AI）。\n"
    "企业知识库助手只服务 IT运维/HR/OA/产品/合规等工作问题；"
    "对写诗/股票/医疗/天气/游戏/算命/翻译/菜谱等非工作问题应拒答并引导替代方案。\n\n"
    "你的任务：给定一个用户问题和一个助手回复，若回复硬答了非工作问题（未拒答而直接作答），"
    "按以下宪法原则改写为【拒答+引导替代方案】的理想回复：\n"
    "1. 明确指出该问题不在企业知识库服务范围；\n"
    "2. 给出对应的替代方案或联系入口（如外部工具、相关部门）；\n"
    "3. 语气保持专业礼貌。\n\n"
    "若回复已是正确的拒答+引导，原样输出该回复（不改写）。\n"
    "只输出改写后的最终回复，不要输出批评过程或任何额外说明。"
)


# ============================================================================
# 硬答判定（规则快速过滤，避免对已拒答的回复做无意义改写）
# ============================================================================

def is_hard_answer(user_text: str, response: str) -> bool:
    """判断是否边界硬答（需 CAI 改写）。

    边界问题 + 无拒答信号 = 硬答（policy 没拒答而直接作答 → 需改写）。
    边界问题 + 有拒答信号 = 已正确拒答 → 跳过（不改写，不构成偏好对）。
    非边界问题 → 跳过（CAI 只针对边界硬答，工作问题无需改写）。

    边界匹配只看【问题】文本（extract_question_text），不看 RAG context（评审 #1）。
    """
    question = extract_question_text(user_text)
    is_boundary = any(kw in question for kw in BOUNDARY_KEYWORDS)
    has_refusal = any(sig in response for sig in REFUSAL_KEYWORDS)
    return is_boundary and not has_refusal


# ============================================================================
# Critic 改写器（本地 7B，按 CAI 宪法改写硬答）
# ============================================================================

class CriticRewriter:
    """本地 critic 模型：按 CAI 宪法改写硬答回复为"拒答+引导"版。

    加载一份 7B（与 policy 各占一份显存）：1.5B policy(~3G) + 7B critic(~14G)
    共 ~17G，M3 Max 36G / AutoDL 4090 24G / A100 40G 均可跑。

    改写失败（生成空/异常）时返回空串，主流程跳过该条不构成偏好对。
    """

    def __init__(self, critic_model: str = "models/Qwen2.5-7B-Instruct"):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        use_bf16 = _detect_bf16()
        logger.info("加载 critic 改写模型: %s (bf16=%s)", critic_model, use_bf16)
        self.tokenizer = AutoTokenizer.from_pretrained(critic_model, trust_remote_code=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModelForCausalLM.from_pretrained(
            critic_model,
            torch_dtype=torch.bfloat16 if use_bf16 else torch.float16,
            device_map="auto",
            trust_remote_code=True,
        )
        self.model.eval()
        logger.info("critic 改写模型加载完成")

    def rewrite(self, user_text: str, response: str, max_new_tokens: int = 256) -> str:
        """按 CAI 宪法改写回复，返回改写后文本。失败返回空串。

        贪心解码（do_sample=False）保证改写稳定可复现。
        """
        import torch
        user_msg = (
            f"问题：{user_text}\n\n"
            f"助手回复：{response}\n\n"
            f"请按宪法原则改写（已是正确拒答则原样输出）："
        )
        msgs = [
            {"role": "system", "content": CAI_REWRITE_SYS},
            {"role": "user", "content": user_msg},
        ]
        text = self.tokenizer.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=False)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        rewritten = self.tokenizer.decode(
            out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        return rewritten


# ============================================================================
# Policy 单候选采样（temperature 0.7 造自然硬答）
# ============================================================================

def sample_one(model, tokenizer, prompt: list[dict],
               max_new_tokens: int = 256, temperature: float = 0.7) -> str:
    """policy 对单个 prompt 采样 1 个回复。

    temperature 0.7（比 generate_rlaif_data 的 0.3/1.1 中间值）造自然硬答：
    既不像 0.3 过度保守趋同，也不像 1.1 过度发散难解析。
    """
    import torch
    text = tokenizer.apply_chat_template(prompt, add_generation_prompt=True, tokenize=False)
    inputs = tokenizer(text, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=True,
            temperature=temperature,
            top_p=0.95,
            pad_token_id=tokenizer.eos_token_id,
        )
    return tokenizer.decode(
        out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()


# ============================================================================
# 边界 prompt 池构建（CAI 只针对边界硬答，不用 RAG/企业聚焦类）
# ============================================================================

def build_boundary_prompt_pool(seed: int = 42) -> list[dict]:
    """构建边界 prompt 池：generate_dpo_data 的边界类 + 额外边界变体。

    返回 [{"prompt": [...], "meta": {"type": "boundary", ...}}]。
    原始边界类约 240 条；额外边界变体（写诗/玄学/游戏/菜谱等）+200 条 → ~440 条。
    """
    rng = random.Random(seed)
    boundary_pairs = generate_boundary_pairs(rng)
    prompts = [{"prompt": p["prompt"], "meta": p["meta"]} for p in boundary_pairs]
    prompts.extend(build_extra_boundary_prompts())
    rng.shuffle(prompts)
    logger.info("边界 prompt 池构建完成：%d 条（原始边界 + 额外变体）", len(prompts))
    return prompts


# ============================================================================
# 主流程
# ============================================================================

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Constitutional AI 改写扩 DPO 数据（critic 按宪法改写硬答）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", default="data/open/dpo_cai.jsonl", help="输出 jsonl 路径")
    parser.add_argument("--count", type=int, default=300, help="生成偏好对数量")
    # 候选生成（policy）
    parser.add_argument("--policy_model", default="models/Qwen2.5-1.5B-Instruct",
                        help="候选生成用的本地模型（HF repo 或本地路径）")
    parser.add_argument("--sft_adapter", default=None,
                        help="可选：SFT/DPO adapter 路径，提供时先 merge 进基座再生成候选")
    parser.add_argument("--max_new_tokens", type=int, default=256, help="候选/改写回复最大长度")
    parser.add_argument("--temperature", type=float, default=0.7,
                        help="policy 采样温度（造自然硬答）")
    # critic 改写
    parser.add_argument("--critic_model", default="models/Qwen2.5-7B-Instruct",
                        help="CAI 改写用的 critic 模型路径（本地 7B）")
    # 断点续传
    parser.add_argument("--checkpoint_every", type=int, default=30, help="每 N 条存盘一次")
    parser.add_argument("--resume", action="store_true", help="断点续传：跳过已产出的 prompt")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # ---- 1. prompt 池 ----
    prompt_pool = build_boundary_prompt_pool(seed=args.seed)
    logger.info("边界 prompt 池 %d 条，目标产出 %d 对", len(prompt_pool), args.count)

    # ---- 2. 断点续传 ----
    if args.resume:
        seen = load_existing(args.output)
    else:
        seen = set()
        # 非 resume 模式但文件已有内容：覆盖
        from pathlib import Path
        Path(args.output).unlink(missing_ok=True)

    # ---- 3. 加载 policy + critic ----
    logger.info("加载 policy model: %s", args.policy_model)
    model, tokenizer = load_policy_model(args.policy_model, args.sft_adapter)
    rewriter = CriticRewriter(critic_model=args.critic_model)

    # ---- 4. 采样 → 判硬答 → 改写 → 存盘 ----
    produced = len(seen)
    skipped_not_hard = 0      # 非硬答（已拒答或非边界）→ 跳过
    skipped_same = 0          # 改写后与原文相同（critic 认为无需改写）→ 跳过
    skipped_empty = 0         # 改写失败（空串）→ 跳过
    rng = random.Random(args.seed)
    buffer: list[dict] = []

    pool_idx = 0
    consecutive_skips = 0
    while produced < args.count:
        if consecutive_skips >= len(prompt_pool) * 2:
            logger.warning(
                "连续 %d 个 prompt 未产出（池已耗尽或硬答率过低），提前结束于 %d/%d",
                consecutive_skips, produced, args.count,
            )
            break

        prompt_item = prompt_pool[pool_idx % len(prompt_pool)]
        pool_idx += 1
        prompt = prompt_item["prompt"]
        meta = prompt_item["meta"]
        ph = _prompt_hash(prompt)
        if args.resume and ph in seen:
            consecutive_skips += 1
            continue

        # ---- 4a. policy 采样 1 回复 ----
        response = sample_one(
            model, tokenizer, prompt,
            max_new_tokens=args.max_new_tokens, temperature=args.temperature,
        )
        if not response:
            skipped_empty += 1
            consecutive_skips += 1
            continue

        # ---- 4b. 判断是否硬答（非硬答跳过，不做无意义改写）----
        user_text = last_user_content(prompt)
        if not is_hard_answer(user_text, response):
            skipped_not_hard += 1
            consecutive_skips += 1
            continue

        # ---- 4c. critic 按 CAI 宪法改写 ----
        rewritten = rewriter.rewrite(
            user_text, response, max_new_tokens=args.max_new_tokens)
        if not rewritten:
            skipped_empty += 1
            consecutive_skips += 1
            continue
        # 改写后与原文相同 = critic 认为无需改写（原回复已合规）→ 不构成偏好对
        if rewritten.strip() == response.strip():
            skipped_same += 1
            consecutive_skips += 1
            continue

        # ---- 4d. 组装偏好对（chosen=改写版, rejected=原硬答）----
        pair = {
            "prompt": prompt,
            "chosen": [{"role": "assistant", "content": rewritten}],
            "rejected": [{"role": "assistant", "content": response}],
            "meta": {**meta, "source": "cai", "critic": args.critic_model},
        }

        buffer.append(pair)
        seen.add(ph)
        produced += 1
        consecutive_skips = 0

        # 存盘
        if len(buffer) >= args.checkpoint_every:
            for p in buffer:
                append_pair(args.output, p)
            buffer.clear()
            logger.info("进度：%d/%d（跳过 非硬答=%d 改写无变化=%d 空=%d）",
                        produced, args.count, skipped_not_hard, skipped_same, skipped_empty)

    # 4e. 写入剩余 buffer
    for p in buffer:
        append_pair(args.output, p)

    # ---- 5. 统计 ----
    logger.info("===== CAI 数据生成完成 =====")
    logger.info("文件: %s", args.output)
    logger.info("总数: %d 对", produced)
    logger.info("跳过: 非硬答=%d, 改写无变化=%d, 空回复=%d",
                skipped_not_hard, skipped_same, skipped_empty)
    logger.info("critic: %s", args.critic_model)
    logger.info("下一步: python scripts/finetune/train_dpo.py --data %s "
                "--base_model %s --output_dir outputs/dpo-cai", args.output, args.policy_model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
