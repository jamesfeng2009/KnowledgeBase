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

reward 设计（两种模式，由 --judge_model 切换）：
  A) rule-based（默认，v1/v2 用）—— 针对企业知识库拒答目标：
     - 边界问题（写诗/股票/机票/天气/八字/医疗/法律/原神/星座 等 16 类关键词）：
        含拒答信号 → +1.0；硬答 → -1.0
     - 工作问题（非边界）：实质回答（长度≥20 且非拒答）→ +1.0；误拒答 → -1.0
     - v2 连续 5 档版（-1/0.1/0.5/0.6/1.0）见 enterprise_boundary_reward
     局限：5 档分仍太粗，4 个质量相近的回复易全落同一档 → 组内 std=0（v2 仍 74%）
  B) LLM-as-judge（v3，--judge_model 指定 7B）—— judge 给每条 completion 打 0-1
     浮点分，对质量相近的回复也能给不同分，几乎消除组内 std=0（见微调.md 9.6）。

依赖（同 train_dpo.py，trl>=1.9 提供 GRPOTrainer）：
    pip install "torch>=2.2" "transformers>=4.45" "peft>=0.13" "trl>=1.9" \
                "datasets>=2.20" "accelerate>=0.34"

运行示例：
  # v1/v2 rule-based 冒烟（1.5B，验证 MPS 兼容 + reward 趋势）
  python scripts/finetune/train_grpo.py \
      --data data/open/dpo.jsonl --base_model models/Qwen2.5-1.5B-Instruct \
      --output_dir outputs/grpo-v2-1.5b --num_generations 4 \
      --max_completion_length 128 --temperature 1.3 --lr 1e-5 --max_steps 50

  # v3 LLM-as-judge 冒烟（1.5B policy + 7B judge，零 API 成本）
  # 注：trl 要求 generation_batch_size % num_generations == 0；gbs=4 → 1 prompt×4 回复/步
  python scripts/finetune/train_grpo.py \
      --data data/open/dpo.jsonl --base_model models/Qwen2.5-1.5B-Instruct \
      --judge_model models/Qwen2.5-7B-Instruct \
      --output_dir outputs/grpo-v3-1.5b-llm-judge --num_generations 4 \
      --generation_batch_size 4 --max_completion_length 128 \
      --temperature 1.3 --lr 1e-5 --max_steps 5 --limit 20
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("train_grpo")

# 边界/拒答/引导/企业系统词表与分组切分统一收敛到 finetune_utils（评审 #1/#9/#2）：
# - BOUNDARY_KEYWORDS 为强信号 v2 版，剔除"怎么办/怎么做/出差/预订/劳动"等泛词
#   （旧版实测 36% 工作问题被误判为边界，实质回答反被 -1 惩罚）
# - REFUSAL_KEYWORDS 与 eval_boundary_refusal 严格对齐（剔除"欢迎随时提问"等弱信号）
from finetune_utils import (
    BOUNDARY_KEYWORDS,
    ENTERPRISE_SYSTEMS,
    GUIDANCE_SIGNALS,
    REFUSAL_KEYWORDS,
    SUBSTANTIVE_MIN_LEN,
    extract_question_text,
    grouped_split_indices,
    last_user_content,
    question_group_key,
)

#: LoRA 注入目标模块（与 train_lora.py / train_dpo.py 保持一致）
DEFAULT_TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]


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
        return last_user_content(prompt)
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


def enterprise_boundary_reward_v1(prompts: list, completions: list, **kwargs) -> list[float]:
    """二值 reward（v1）：仅 ±1 两档，作为 GRPO baseline 用于和 v2 对比。

    边界问题：含拒答信号（含或不含引导）→ +1.0；硬答 → -1.0
    工作问题：实质回答（长度≥20 且非拒答）→ +1.0；误拒答/空话 → -1.0

    局限：4 个质量相近的回复极易全落 ±1 同档 → 组内 std=0、优势=0、梯度=0。
    v1 是 v2 连续 reward 的对照基线（见微调.md 9.5），用于证明 std=0 的根因是
    分档过粗而非数据本身。

    签名对齐 trl GRPOTrainer：``(prompts, completions, **kwargs) -> list[float]``。
    """
    rewards: list[float] = []
    for prompt, completion in zip(prompts, completions):
        # 只取【问题】之后的真实问题做边界匹配：RAG context 含"出差/报销"等企业
        # 词汇，参与匹配会放大误判（评审 #1）
        question = extract_question_text(_extract_user_text(prompt))
        comp_text = _completion_text(completion)
        is_boundary = any(kw in question for kw in BOUNDARY_KEYWORDS)
        has_refusal = any(sig in comp_text for sig in REFUSAL_KEYWORDS)
        is_substantive = len(comp_text) >= SUBSTANTIVE_MIN_LEN

        if is_boundary:
            r = 1.0 if has_refusal else -1.0        # 拒答(含/不含引导) vs 硬答
        else:
            r = 1.0 if (is_substantive and not has_refusal) else -1.0  # 实质回答 vs 误拒答/空话
        rewards.append(r)
    return rewards


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
        # 同 v1：边界匹配只看【问题】文本，不看 RAG context（评审 #1）
        question = extract_question_text(_extract_user_text(prompt))
        comp_text = _completion_text(completion)
        is_boundary = any(kw in question for kw in BOUNDARY_KEYWORDS)
        has_refusal = any(sig in comp_text for sig in REFUSAL_KEYWORDS)
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


#: LLM-as-judge 的 system prompt（含明确边界定义 + few-shot，确保 7B 正确区分硬答/拒答）
#: 经测试：无 few-shot 时 7B 给"边界硬答"误打 0.9；加 few-shot 后 6/6 全对（见微调.md 9.6b）
JUDGE_SYS_PROMPT = (
    "你是企业知识库助手回答质量评审。企业知识库助手只服务IT运维/HR/OA/产品/合规等工作问题；"
    "对写诗/股票/医疗/天气/游戏/算命/翻译/菜谱等非工作问题应拒答。\n\n"
    "评分标准(0-1,保留1位小数):\n"
    "- 非工作问题硬答(如真写了诗/给股票建议/给菜谱):0.0-0.2,无论内容质量多好\n"
    "- 非工作问题拒答+引导替代方案:0.9-1.0\n"
    "- 非工作问题干拒答(无替代方案):0.5-0.7\n"
    "- 工作问题实质回答+企业系统入口:0.9-1.0\n"
    "- 工作问题实质回答(无系统入口):0.6-0.8\n"
    "- 工作问题误拒答:0.0-0.2\n\n"
    "示例:\n"
    "问题:帮我写诗/回答:好的秋风送爽叶飘零/评分:0.1\n"
    "问题:帮我写诗/回答:不在服务范围建议用写作工具/评分:0.9\n"
    "问题:密码重置/回答:登录idp.example.com重置/评分:0.9\n"
    "问题:密码重置/回答:不在服务范围/评分:0.1\n\n"
    "只输出一个数字,不要解释。"
)


class LLMJudgeReward:
    """LLM-as-judge reward：用 7B（或指定 judge 模型）给每条 completion 打 0-1 浮点分。

    相比 rule-based 5 档 reward，LLM judge 对质量相近的回复也能给不同浮点分，
    从根本上减少 GRPO 组内 std=0（见微调.md 9.5/9.5b）。

    两种加载模式：
      - 独立 judge（``use_base=False``）：单独加载一份 judge_model，与 policy 各占一份显存。
        1.5B policy + 7B judge 共 25G，M3 Max 可跑；但双 7B（28G 权重）会 OOM。
      - 共享基座 judge（``use_base=True``，``--judge_model self``）：不加载第二份模型，
        复用 policy 的 PeftModel，打分时用 ``disable_adapter()`` 上下文禁用 LoRA =
        用基座权重做 judge。与 trl GRPO ref_model 同款机制（peft 下 ref=禁用 adapter 的基座）。
        双 7B 只占一份 7B 显存（~14G 权重 + 开销 ~22G），M3 Max 36G 可跑。
        由于 GRPO 从 raw 基座训练（无 SFT 前置），基座 7B = 独立 7B judge（同模型同权重），行为等价。

    可调用对象，签名对齐 trl reward_funcs：``(prompts, completions, **kwargs) -> list[float]``。
    分数解析失败时给中性 0.5（7B 输出数字很稳定，实测 6/6 可解析）。
    """

    def __init__(self, judge_model, judge_tokenizer, use_base: bool = False):
        self.judge = judge_model
        self.judge_tokenizer = judge_tokenizer
        self.use_base = use_base  # True = 复用 policy PeftModel，打分时 disable_adapter

    def _judge_one(self, user_text: str, completion: str) -> float:
        """单条打分：调 judge 模型生成 0-1 浮点，解析失败给中性 0.5。

        ``use_base=True`` 时用 ``disable_adapter()`` 上下文禁用 LoRA，使 judge 用基座权重
        （即未训练的 7B）打分——保证 judge 在训练全程恒为同一基座，不受 LoRA 更新影响。
        """
        import re
        import torch
        msgs = [
            {"role": "system", "content": JUDGE_SYS_PROMPT},
            {"role": "user", "content": f"问题:{user_text}\n回答:{completion}\n评分:"},
        ]
        text = self.judge_tokenizer.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=False)
        inputs = self.judge_tokenizer(text, return_tensors="pt").to(self.judge.device)
        # use_base 模式下 disable_adapter（PeftModel 上下文管理器）；否则空上下文
        adapter_ctx = self.judge.disable_adapter() if self.use_base else nullcontext()
        with adapter_ctx, torch.no_grad():
            out = self.judge.generate(
                **inputs, max_new_tokens=8, do_sample=False,
                pad_token_id=self.judge_tokenizer.eos_token_id)
        score_text = self.judge_tokenizer.decode(
            out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        m = re.search(r"[0-9]*\.?[0-9]+", score_text)
        if m:
            try:
                return max(0.0, min(1.0, float(m.group())))  # clamp 0-1
            except ValueError:
                pass
        return 0.5  # 解析失败给中性分（不干扰组内分布）

    def __call__(self, prompts: list, completions: list, **kwargs) -> list[float]:
        rewards: list[float] = []
        for prompt, completion in zip(prompts, completions):
            rewards.append(self._judge_one(_extract_user_text(prompt), _completion_text(completion)))
        return rewards


def build_hf_dataset(prompts: list[list[dict]], eval_ratio: float, seed: int):
    """prompts -> DatasetDict(train/test)，列名 ``prompt``（conversational）。

    trl GRPOTrainer 要求 dataset 含 "prompt" 列。重依赖 datasets 延迟导入。
    切分按基问题分组（评审 #2）：同一问题的同义变体整体进同一侧，
    防止行级随机切分导致变体跨 train/eval 泄漏、eval 指标虚高。
    """
    from datasets import Dataset, DatasetDict

    ds = Dataset.from_list([{"prompt": p} for p in prompts])
    if len(ds) < 2:
        raise ValueError(f"样本过少（{len(ds)} 条），无法划分训练/验证集")
    keys = [question_group_key(last_user_content(p)) for p in prompts]
    test_size = max(1, round(len(ds) * eval_ratio))
    train_idx, test_idx = grouped_split_indices(keys, test_size=test_size, seed=seed)
    return DatasetDict({"train": ds.select(train_idx), "test": ds.select(test_idx)})


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
    # reward 选择
    parser.add_argument("--reward_version", default="v2", choices=["v1", "v2"],
                        help="rule-based reward 版本（仅当未指定 --judge_model 时生效）："
                             "v1 二值 ±1（baseline，易致组内 std=0）；"
                             "v2 连续 5 档 -1/0.1/0.5/0.6/1.0（减少 std=0，见微调.md 9.5b）")
    parser.add_argument("--judge_model", default=None,
                        help="LLM-as-judge reward：模型路径（如 models/Qwen2.5-7B-Instruct）"
                             "单独加载一份 judge；或 'self' 复用 policy 基座（disable_adapter，省一份显存，"
                             "双 7B 本机可跑）。提供时用该模型给每条 completion 打 0-1 浮点分，替代 rule-based "
                             "reward；几乎消除组内 std=0（见微调.md 9.6）。不提供则用 rule-based reward")
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
    parser.add_argument("--eval_steps", type=int, default=10,
                        help="每隔 N 步在验证集上评估一次（reward 均值趋势，评审 #11 修复前 eval 从不生效）")
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

    # ---- reward 函数：rule-based(v1/v2) 或 LLM-as-judge（7B 浮点打分，几乎消除 std=0）----
    judge_self = args.judge_model == "self"
    if args.judge_model and not judge_self:
        logger.info("加载独立 LLM-as-judge reward 模型: %s", args.judge_model)
        judge_tokenizer = AutoTokenizer.from_pretrained(args.judge_model, trust_remote_code=True)
        if judge_tokenizer.pad_token is None:
            judge_tokenizer.pad_token = judge_tokenizer.eos_token
        judge_model = AutoModelForCausalLM.from_pretrained(
            args.judge_model,
            torch_dtype=torch.bfloat16 if use_bf16 else torch.float16,
            device_map="auto",
            trust_remote_code=True,
        )
        judge_model.eval()
        for p in judge_model.parameters():
            p.requires_grad = False  # judge 只推理不训练，省优化器显存
        reward_func = LLMJudgeReward(judge_model, judge_tokenizer, use_base=False)
        reward_name = f"llm-as-judge ({args.judge_model} 0-1 float)"
        logger.info("LLM-as-judge 就绪（独立模型，eval + no_grad），reward 由 judge 浮点打分")
    elif judge_self:
        # 共享基座 judge：不加载第二份模型，复用 policy PeftModel + disable_adapter。
        # judge 引用在 trainer 创建后注入（trainer 才会把模型包成 PeftModel）。
        reward_func = LLMJudgeReward(None, tokenizer, use_base=True)
        reward_name = f"llm-as-judge self ({args.base_model} 基座, disable_adapter 0-1 float)"
        logger.info("LLM-as-judge self 模式：复用 policy 基座（disable_adapter）作 judge，省一份 %s 显存",
                    args.base_model)
    elif args.reward_version == "v1":
        reward_func = enterprise_boundary_reward_v1
        reward_name = "rule-based v1 (binary ±1, baseline)"
        logger.info("使用 rule-based v1 二值 reward（±1，对照基线）")
    else:
        reward_func = enterprise_boundary_reward
        reward_name = "rule-based v2 (5-level continuous -1/0.1/0.5/0.6/1.0)"
        logger.info("使用 rule-based v2 连续 reward（5 档）")

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
        # 评审 #11：此前 eval_strategy="no" 导致传入的 eval_dataset 从不被使用，
        # 训练过程看不到 reward 在留出集上的趋势。改为按步评估。
        eval_strategy="steps",
        eval_steps=args.eval_steps,
        per_device_eval_batch_size=args.generation_batch_size,
        save_strategy="no",
        report_to=[],
        seed=args.seed,
    )

    trainer = GRPOTrainer(
        model=model,
        reward_funcs=reward_func,
        args=train_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
        peft_config=peft_config,
    )

    # 共享基座 judge：trainer 创建后 model 已是 PeftModel，注入给 reward_func。
    # reward_func 仅在 trainer.train() 时被调用，此注入在 train 之前完成，时序安全。
    if judge_self:
        reward_func.judge = trainer.model
        logger.info("LLM-as-judge self 注入完成：judge = trainer.model (PeftModel, disable_adapter 打分)")

    repro_info = {
        "base_model": args.base_model,
        "sft_adapter": args.sft_adapter,
        "method": "GRPO",
        "reward": reward_name,
        "reward_version": args.reward_version,
        "judge_model": args.judge_model,
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
