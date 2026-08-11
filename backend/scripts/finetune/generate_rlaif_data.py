#!/usr/bin/env python
"""RLAIF 自动标注扩 DPO 数据 — 用强模型当裁判做 pairwise 偏好标注。

标准 RLAIF 流程（Zephyr/Llama-3 同款，见微调.md 第十章 10.5）：
    1. prompt 池：复用 generate_dpo_data 的弱 prompt 构造（边界/RAG/企业聚焦三类）
    2. 候选生成：本地 policy model 对每个 prompt 采样 2 个回复（不同 temperature 造差异）
    3. 强模型裁判：调 API（Claude）pairwise 判定哪个更好；无 API 时 fallback 规则裁判
    4. 输出 chosen/rejected，扩到 5k-10k 对

动机（见微调.md 10.5）：generate_dpo_data.py 规则化构造只能产 ~600 对（受限于手写
模板数量），且 chosen/rejected 是人工预设的"好/坏"，覆盖面窄。RLAIF 用模型生成 +
强模型裁判，能扩量到 5k-10k，并引入更自然的回复差异 + 覆盖更多边界变体。

两种裁判（可插拔，--judge 控制）：
    - api（默认）：调 Anthropic Claude，pairwise 判定 A/B/TIE。需 ANTHROPIC_API_KEY。
      适合正式扩量，裁判质量高（强模型语义理解）。
    - rule：规则裁判 fallback，复用 train_grpo.py 的 reward 逻辑（边界关键词 + 拒答信号
      + 企业系统入口）打分，高分者为 chosen。零 API 成本，适合无 key 时冒烟验证流程。
      局限：规则裁判只能判离散档位，和 generate_dpo_data 的预设 chosen/rejected 同源，
      扩量价值有限——真正价值在 api 裁判。

断点续传：每 --checkpoint_every 条存盘，--resume 时跳过已产出 prompt（按 prompt 哈希去重）。

依赖：
    pip install "torch>=2.2" "transformers>=4.45" "peft>=0.13"  # 候选生成
    pip install "anthropic>=0.40"                                # API 裁判（可选，无则 fallback rule）

用法：
    # 冒烟（100 对，规则裁判，零 API 成本，验证流程连通）
    python scripts/finetune/generate_rlaif_data.py \\
        --output data/open/dpo_rlaif.jsonl --count 100 --judge rule \\
        --policy_model models/Qwen2.5-1.5B-Instruct

    # 正式（5k 对，Claude 裁判，扩边界变体覆盖）
    python scripts/finetune/generate_rlaif_data.py \\
        --output data/open/dpo_rlaif.jsonl --count 5000 --judge api \\
        --policy_model models/Qwen2.5-7B-Instruct \\
        --sft_adapter outputs/dpo-v2-7b

    # 断点续传（中途中断后接着跑）
    python scripts/finetune/generate_rlaif_data.py \\
        --output data/open/dpo_rlaif.jsonl --count 5000 --judge api \\
        --policy_model models/Qwen2.5-7B-Instruct --resume
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Any, Iterator

# 复用 generate_dpo_data 的 prompt 构造（三类偏好对的 prompt 部分）
from generate_dpo_data import (
    SYSTEM_PROMPT,
    generate_boundary_pairs,
    generate_enterprise_focus_pairs,
    generate_rag_extraction_pairs,
)
from generate_embedding_data import _query_variants
# 边界/拒答/引导/企业系统词表统一收敛到 finetune_utils（评审 #1/#9：
# 三处各自维护且口径不一致，规则裁判沿用旧泛词表会把 36% 工作问题误判为边界）
from finetune_utils import (
    BOUNDARY_KEYWORDS,
    ENTERPRISE_SYSTEMS,
    GUIDANCE_SIGNALS,
    REFUSAL_KEYWORDS,
    SUBSTANTIVE_MIN_LEN,
    extract_question_text,
    last_user_content,
)

logger = logging.getLogger("generate_rlaif")

# ============================================================================
# 扩 prompt 池：补充边界变体（解决 B1 写诗硬答 + 增加覆盖面）
# ============================================================================

#: 额外边界问题（原始 16 类不够，补充"写诗"变体 + 更多边界类别）
#: RLAIF 只需"问题"，chosen/rejected 由流程自动生成
_EXTRA_BOUNDARY_QUESTIONS: list[tuple[str, str]] = [
    # ---- 写诗变体（B1 写诗硬答，需更多覆盖）----
    ("帮我写一首打油诗", "creative_writing"),
    ("帮我写首藏头诗", "creative_writing"),
    ("帮我写一段 Rap", "creative_writing"),
    ("帮我写几句顺口溜", "creative_writing"),
    ("帮我作首关于春天的诗", "creative_writing"),
    ("帮我写首现代诗", "creative_writing"),
    ("帮我写首情诗", "creative_writing"),
    ("帮我写个歌词", "creative_writing"),
    ("帮我写首词", "creative_writing"),
    ("帮我写个小故事", "creative_writing"),
    ("帮我写封情书", "creative_writing"),
    ("帮我写个剧本", "creative_writing"),
    # ---- 玄学/命理变体 ----
    ("帮我查查今天的星座运势", "fortune_telling"),
    ("帮我算算塔罗牌", "fortune_telling"),
    ("帮我看看手相", "fortune_telling"),
    ("帮我选个吉日", "fortune_telling"),
    ("帮我算算我和他的缘分", "fortune_telling"),
    # ---- 游戏/娱乐 ----
    ("帮我推荐一款好玩的游戏", "entertainment"),
    ("原神怎么配队", "entertainment"),
    ("帮我推荐一部好看的电影", "entertainment"),
    ("帮我讲个笑话", "entertainment"),
    # ---- 生活/菜谱 ----
    ("红烧肉怎么做", "recipe"),
    ("帮我写个菜谱", "recipe"),
    ("公司附近有什么好吃的", "restaurant"),
    # ---- 购物/比价 ----
    ("帮我比价一下iPhone", "shopping"),
    ("iPhone 值得买吗", "shopping"),
    # ---- 其他边界 ----
    ("帮我分析一下我的梦境", "dream_analysis"),
    ("帮我起个名字", "naming"),
    ("帮我做个心理测试", "psychology"),
    ("帮我画幅画", "creative_writing"),
    ("帮我唱首歌", "entertainment"),
]


def build_extra_boundary_prompts() -> list[dict]:
    """构建额外边界 prompt（补充 _EXTRA_BOUNDARY_QUESTIONS 的变体）。

    每条问题经 _query_variants 生成 5-9 个变体，30 条 × 7 ≈ 200+ 条额外 prompt。
    这些 prompt 只有问题部分，chosen/rejected 由 RLAIF 流程自动生成。
    """
    prompts: list[dict] = []
    for question, category in _EXTRA_BOUNDARY_QUESTIONS:
        for variant in _query_variants(question):
            prompts.append({
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": variant},
                ],
                "meta": {"type": "boundary", "category": category, "source": "extra"},
            })
    logger.info("额外边界 prompt 生成完成：%d 条（写诗/玄学/游戏/菜谱等变体）", len(prompts))
    return prompts

#: 裁判 system prompt（pairwise 判定，明确边界定义 + 输出格式约束）
#: 设计要点：① 明确边界问题应拒答 ② 工作问题应实质回答 ③ 只输出 A/B/TIE 不解释
JUDGE_PAIRWISE_SYS = (
    "你是企业知识库助手回答质量评审，负责比较两个回复的优劣。\n"
    "企业知识库助手只服务IT运维/HR/OA/产品/合规等工作问题；"
    "对写诗/股票/医疗/天气/游戏/算命/翻译/菜谱等非工作问题应拒答+引导替代方案。\n\n"
    "评判标准（按优先级）：\n"
    "1. 边界问题（非工作）：拒答+引导 > 干拒答 > 硬答（硬答再好也是错的）\n"
    "2. 工作问题：实质回答+企业系统入口 > 实质回答 > 短/空话 > 误拒答\n"
    "3. 安全性：编造信息/外部服务引导 > 无引导；两者都错时选危害小的\n\n"
    "只输出一个字母：A、B 或 TIE（两者质量相当）。不要解释。"
)


def _extract_user_text(prompt: Any) -> str:
    """从 conversational prompt 提取最后一条 user content。"""
    if isinstance(prompt, list):
        return last_user_content(prompt)
    return str(prompt)


# ============================================================================
# 裁判策略（可插拔）
# ============================================================================

class RuleJudge:
    """规则裁判 fallback：用 reward 逻辑给两个回复打分，高分者为 chosen。

    复用 train_grpo.py 的连续 5 档 reward 逻辑（边界拒答/工作实质回答）。
    零 API 成本，适合无 key 时冒烟验证 RLAIF 流程。
    局限：离散分档，和 generate_dpo_data 的预设 chosen/rejected 同源，扩量价值有限。
    """

    @staticmethod
    def _score(user_text: str, completion: str) -> float:
        """单条回复打分（连续 5 档，对齐 train_grpo enterprise_boundary_reward v2）。

        边界匹配只看【问题】文本（extract_question_text），不看 RAG context（评审 #1）。
        """
        question = extract_question_text(user_text)
        is_boundary = any(kw in question for kw in BOUNDARY_KEYWORDS)
        has_refusal = any(sig in completion for sig in REFUSAL_KEYWORDS)
        has_guidance = any(sig in completion for sig in GUIDANCE_SIGNALS)
        has_enterprise = any(s in completion for s in ENTERPRISE_SYSTEMS)
        is_substantive = len(completion) >= SUBSTANTIVE_MIN_LEN

        if is_boundary:
            if has_refusal and has_guidance:
                return 1.0    # 拒答+引导（最佳）
            elif has_refusal:
                return 0.5    # 干拒答
            else:
                return -1.0   # 硬答（最差）
        else:
            if has_refusal:
                return -1.0   # 工作问题误拒答
            elif has_enterprise and is_substantive:
                return 1.0    # 实质+企业入口
            elif is_substantive:
                return 0.6    # 实质但无入口
            else:
                return 0.1    # 短/空话

    def judge(self, user_text: str, resp_a: str, resp_b: str) -> str:
        """返回 'A' / 'B' / 'TIE'。"""
        sa, sb = self._score(user_text, resp_a), self._score(user_text, resp_b)
        if sa > sb + 0.05:
            return "A"
        if sb > sa + 0.05:
            return "B"
        return "TIE"


class APIJudge:
    """Anthropic Claude 裁判：调 Messages API pairwise 判定 A/B/TIE。

    强模型语义理解，能区分规则裁判无法判定的细微质量差异（如话术自然度、引导恰当性）。
    需 anthropic SDK + ANTHROPIC_API_KEY。失败时返回 'TIE'（不干扰流程，但会记 warning）。
    """

    def __init__(self, model: str = "claude-sonnet-4-6-20250514", max_retries: int = 3):
        try:
            import anthropic  # 延迟导入：无 SDK 时 fallback RuleJudge
        except ImportError as exc:
            raise ImportError(
                "API 裁判需要 anthropic SDK：pip install anthropic。"
                "或用 --judge rule 走规则裁判（零依赖）。"
            ) from exc
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("API 裁判需要环境变量 ANTHROPIC_API_KEY。或用 --judge rule。")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_retries = max_retries

    def judge(self, user_text: str, resp_a: str, resp_b: str) -> str:
        """调 Claude pairwise 判定，返回 'A' / 'B' / 'TIE'。失败返回 'TIE'。"""
        user_msg = (
            f"问题：{user_text}\n\n"
            f"回复A：{resp_a}\n\n"
            f"回复B：{resp_b}\n\n"
            f"哪个回复更好？只输出 A、B 或 TIE。"
        )
        for attempt in range(self.max_retries):
            try:
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=8,
                    system=JUDGE_PAIRWISE_SYS,
                    messages=[{"role": "user", "content": user_msg}],
                )
                text = resp.content[0].text.strip().upper()
                if "A" in text and "B" not in text:
                    return "A"
                if "B" in text and "A" not in text:
                    return "B"
                if "TIE" in text or ("A" in text and "B" in text):
                    return "TIE"
                # 未识别输出，重试
                logger.warning("裁判输出未识别（attempt %d）：%r", attempt + 1, text)
            except Exception as exc:
                logger.warning("裁判 API 调用失败（attempt %d）：%s", attempt + 1, exc)
        logger.warning("裁判 %d 次重试均失败，返回 TIE", self.max_retries)
        return "TIE"


class LocalJudge:
    """本地 7B pairwise 裁判：零 API 成本，复用 GRPO v3 的 7B judge 经验。

    对边界拒答 vs 硬答的明确判据已验证 6/6 全对（GRPO v3 JUDGE_SYS_PROMPT）；
    这里复用 JUDGE_PAIRWISE_SYS（pairwise 版边界定义），做 A/B/TIE 判定。
    细微质量差异不如 Claude，但 DPO 对 20-30% 标注噪声有鲁棒性。

    独立加载一份 7B（与 policy 各占一份显存）：1.5B policy(~3G) + 7B judge(~14G)
    共 ~17G，M3 Max 36G 可跑。
    """

    def __init__(self, judge_model: str = "models/Qwen2.5-7B-Instruct"):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        use_bf16 = _detect_bf16()
        logger.info("加载本地裁判模型: %s (bf16=%s)", judge_model, use_bf16)
        self.tokenizer = AutoTokenizer.from_pretrained(judge_model, trust_remote_code=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModelForCausalLM.from_pretrained(
            judge_model,
            torch_dtype=torch.bfloat16 if use_bf16 else torch.float16,
            device_map="auto",
            trust_remote_code=True,
        )
        self.model.eval()
        logger.info("本地裁判模型加载完成")

    def judge(self, user_text: str, resp_a: str, resp_b: str) -> str:
        """本地 7B pairwise 判定，返回 'A' / 'B' / 'TIE'。解析失败返回 'TIE'。"""
        import torch
        user_msg = (
            f"问题：{user_text}\n\n"
            f"回复A：{resp_a}\n\n"
            f"回复B：{resp_b}\n\n"
            f"哪个回复更好？只输出 A、B 或 TIE。"
        )
        msgs = [
            {"role": "system", "content": JUDGE_PAIRWISE_SYS},
            {"role": "user", "content": user_msg},
        ]
        text = self.tokenizer.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                **inputs, max_new_tokens=8, do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        verdict = self.tokenizer.decode(
            out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip().upper()
        # 复用 APIJudge 的解析逻辑
        if "A" in verdict and "B" not in verdict:
            return "A"
        if "B" in verdict and "A" not in verdict:
            return "B"
        if "TIE" in verdict or ("A" in verdict and "B" in verdict):
            return "TIE"
        logger.warning("本地裁判输出未识别：%r，返回 TIE", verdict)
        return "TIE"


# ============================================================================
# prompt 池构建
# ============================================================================

def build_prompt_pool(seed: int = 42) -> list[dict]:
    """构建 prompt 池：复用 generate_dpo_data 的三类偏好对 + 额外边界变体。

    返回 [{"prompt": [...], "meta": {"type": "...", ...}}]。
    原始三类约 697 条；额外边界变体（写诗/玄学/游戏/菜谱等）+200 条 → ~900 条。
    """
    rng = random.Random(seed)
    all_pairs = (
        generate_boundary_pairs(rng)
        + generate_rag_extraction_pairs(rng)
        + generate_enterprise_focus_pairs(rng)
    )
    prompts = [{"prompt": p["prompt"], "meta": p["meta"]} for p in all_pairs]
    # 追加额外边界变体（解决 B1 写诗硬答 + 增加覆盖面）
    prompts.extend(build_extra_boundary_prompts())
    rng.shuffle(prompts)
    logger.info("prompt 池构建完成：%d 条（原始三类 + 额外边界变体）", len(prompts))
    return prompts


# ============================================================================
# 候选生成（本地 transformers 模型）
# ============================================================================

def load_policy_model(base_model: str, sft_adapter: str | None = None):
    """加载 policy model（基座 + 可选 SFT adapter merge），返回 (model, tokenizer)。

    sft_adapter 提供时先 merge 进基座（与 train_dpo.py 一致的两阶段流水线）。
    """
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    use_bf16 = _detect_bf16()
    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        torch_dtype=torch.bfloat16 if use_bf16 else torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    if sft_adapter:
        logger.info("加载 SFT adapter 并合并: %s", sft_adapter)
        model = PeftModel.from_pretrained(model, sft_adapter)
        model = model.merge_and_unload()

    model.eval()
    return model, tokenizer


def _detect_bf16() -> bool:
    """检测设备是否支持 bf16（CUDA / Apple Silicon MPS）。"""
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


def generate_candidates(model, tokenizer, prompt: list[dict], n: int = 2,
                        max_new_tokens: int = 256) -> list[str]:
    """对单个 prompt 采样 n 个候选回复（不同 temperature 造差异）。

    RLAIF 的核心：同一 prompt 采样多个回复，靠 temperature 差异造自然多样性。
    温度对：0.3（保守低质）+ 1.1（发散高质），让两个候选有可判定的质量差异。

    修复：max_new_tokens 此前硬编码 256，--max_new_tokens 参数无效（batch 优化评审）。
    """
    import torch
    text = tokenizer.apply_chat_template(prompt, add_generation_prompt=True, tokenize=False)
    inputs = tokenizer(text, return_tensors="pt").to(model.device)

    candidates: list[str] = []
    # 温度梯度：从保守到发散，造可判定差异
    temps = [0.3, 1.1] if n == 2 else [0.3 + 0.8 * i / max(n - 1, 1) for i in range(n)]
    for temp in temps[:n]:
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=True,
                temperature=temp,
                top_p=0.95,
                pad_token_id=tokenizer.eos_token_id,
            )
        resp = tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        candidates.append(resp)
    return candidates


def generate_candidates_batch(
    model, tokenizer, prompts: list[list[dict]], n: int = 2,
    max_new_tokens: int = 128,
) -> list[list[str]]:
    """对一批 prompt 批量采样候选回复（batch 加速：GPU 利用率 33%→80%）。

    策略：同一温度的候选一起 batch 生成（左 padding 对齐），n 个温度 = n 次
    batch forward。比逐条 2n 次顺序 forward 快 ~3x（GPU 并行 + 减少 kernel launch）。

    左 padding 原因：生成任务需所有序列在同一位置开始输出新 token，左 padding
    保证 prompt 末尾右侧对齐（padding 在左侧，生成从右侧统一开始）。
    """
    import torch

    texts = [
        tokenizer.apply_chat_template(p, add_generation_prompt=True, tokenize=False)
        for p in prompts
    ]
    # 左 padding：生成任务必须左 padding（右对齐），否则生成位置错乱
    orig_side = tokenizer.padding_side
    tokenizer.padding_side = "left"
    try:
        encodings = tokenizer(texts, return_tensors="pt", padding=True)
    finally:
        tokenizer.padding_side = orig_side  # 恢复，避免污染后续单条调用

    input_ids = encodings["input_ids"].to(model.device)
    attention_mask = encodings["attention_mask"].to(model.device)
    input_len = input_ids.shape[1]  # 含 padding 的统一长度

    temps = [0.3, 1.1] if n == 2 else [0.3 + 0.8 * i / max(n - 1, 1) for i in range(n)]

    all_candidates: list[list[str]] = [[] for _ in prompts]
    for temp in temps[:n]:
        with torch.no_grad():
            out = model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=max_new_tokens,
                do_sample=True,
                temperature=temp,
                top_p=0.95,
                pad_token_id=tokenizer.eos_token_id,
            )
        # out shape: (batch, input_len + new_tokens)。只取生成部分（input_len 之后）
        for i in range(len(prompts)):
            new_tokens = out[i][input_len:]
            resp = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
            all_candidates[i].append(resp)

    return all_candidates


# ============================================================================
# 断点续传
# ============================================================================

def _prompt_hash(prompt: list[dict]) -> str:
    """prompt 内容哈希（用于断点续送去重）。"""
    content = json.dumps(prompt, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(content.encode()).hexdigest()[:12]


def load_existing(path: str | Path) -> set[str]:
    """读取已产出的 prompt 哈希集合（断点续传用）。"""
    path = Path(path)
    if not path.exists():
        return set()
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                seen.add(_prompt_hash(obj["prompt"]))
            except (json.JSONDecodeError, KeyError):
                continue
    logger.info("断点续传：已加载 %d 条已产出记录", len(seen))
    return seen


def append_pair(path: str | Path, pair: dict) -> None:
    """追加单条偏好对到 jsonl（断点续传用追加模式）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pair, ensure_ascii=False) + "\n")


# ============================================================================
# 主流程
# ============================================================================

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RLAIF 自动标注扩 DPO 数据（强模型裁判 pairwise 标注）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", default="data/open/dpo_rlaif.jsonl", help="输出 jsonl 路径")
    parser.add_argument("--count", type=int, default=5000, help="生成偏好对数量")
    # 候选生成
    parser.add_argument("--policy_model", default="models/Qwen2.5-1.5B-Instruct",
                        help="候选生成用的本地模型（HF repo 或本地路径）")
    parser.add_argument("--sft_adapter", default=None,
                        help="可选：SFT/DPO adapter 路径，提供时先 merge 进基座再生成候选")
    parser.add_argument("--n_candidates", type=int, default=2,
                        help="每个 prompt 采样候选数（默认 2，pairwise 裁判）")
    parser.add_argument("--max_new_tokens", type=int, default=256, help="候选回复最大长度")
    # batch 加速（batch 优化评审：GPU 33% → 80%）
    parser.add_argument("--batch_size", type=int, default=1,
                        help="batch 候选生成大小（1=逐条旧逻辑，8=批量加速 ~3x）。"
                             "batch>1 时用 generate_candidates_batch 左 padding 批量生成")
    # 裁判
    parser.add_argument("--judge", choices=["api", "rule", "local"], default="api",
                        help="裁判模式：api=Anthropic Claude（需 key）；"
                             "rule=规则裁判（零成本 fallback）；"
                             "local=本地 7B pairwise 裁判（零 API 成本，复用 GRPO v3 judge 经验）")
    parser.add_argument("--judge_model", default="claude-sonnet-4-6-20250514",
                        help="API 裁判模型（Anthropic model id）")
    parser.add_argument("--local_judge_model", default="models/Qwen2.5-7B-Instruct",
                        help="本地裁判模型路径（仅 --judge local 生效；评审 #13 修复前"
                             "误用 --judge_model 的 Claude model id，本地加载必失败）")
    # 断点续传
    parser.add_argument("--checkpoint_every", type=int, default=50, help="每 N 条存盘一次")
    parser.add_argument("--resume", action="store_true", help="断点续传：跳过已产出的 prompt")
    parser.add_argument("--resume_keep", action="store_true",
                        help="保留已产出数据并从现有计数续跑，但允许 prompt 复用（不跳过已见 "
                             "prompt）。适合 batch 优化续跑：保留已有 750 对 + 生成 4250 对 = "
                             "5000 总。--resume 会跳过已见 prompt（严格去重，~900 池只能产 "
                             "~900 对），--resume_keep 允许 do_sample 复用扩到 5k+")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # ---- 1. 裁判初始化 ----
    if args.judge == "api":
        try:
            judge: RuleJudge | APIJudge = APIJudge(model=args.judge_model)
            logger.info("裁判模式：API（%s）", args.judge_model)
        except (ImportError, ValueError) as exc:
            logger.warning("%s — 自动降级为规则裁判", exc)
            judge = RuleJudge()
            logger.info("裁判模式：规则（fallback）")
    elif args.judge == "local":
        judge: RuleJudge | APIJudge | LocalJudge = LocalJudge(judge_model=args.local_judge_model)
        logger.info("裁判模式：本地 7B pairwise（%s，零 API 成本）", args.local_judge_model)
    else:
        judge = RuleJudge()
        logger.info("裁判模式：规则（零 API 成本，适合冒烟）")

    # ---- 2. prompt 池 ----
    prompt_pool = build_prompt_pool(seed=args.seed)
    logger.info("prompt 池 %d 条，目标产出 %d 对", len(prompt_pool), args.count)

    # ---- 3. 断点续传 ----
    # --resume：严格去重，跳过已见 prompt（~900 池只能产 ~900 对）
    # --resume_keep：保留已有数据 + 从现有计数续跑，允许 prompt 复用（扩到 5k+）
    # 两者都不设：文件有内容则覆盖
    if args.resume_keep:
        seen = load_existing(args.output)
    elif args.resume:
        seen = load_existing(args.output)
    else:
        seen = set()
    if seen and not args.resume and not args.resume_keep:
        # 非 resume 模式但文件已有内容：覆盖
        Path(args.output).unlink(missing_ok=True)
        seen = set()

    # ---- 4. 加载 policy model ----
    logger.info("加载 policy model: %s (batch_size=%d)", args.policy_model, args.batch_size)
    model, tokenizer = load_policy_model(args.policy_model, args.sft_adapter)

    # ---- 5. 生成 + 裁判 ----
    produced = len(seen)
    skipped_same = 0
    skipped_tie = 0
    rng = random.Random(args.seed)
    buffer: list[dict] = []

    use_batch = args.batch_size > 1
    if use_batch:
        logger.info("batch 模式：batch_size=%d，左 padding 批量候选生成", args.batch_size)

    # 循环采样 prompt 池直到达成目标数量
    # 评审 #4：以"连续跳过"为耗尽判据提前 break，防止循环空转永不退出。
    # batch 模式下 consecutive_skips 跨 batch 累计（收集阶段 + 裁判阶段）。
    pool_idx = 0
    consecutive_skips = 0
    while produced < args.count:
        if consecutive_skips >= len(prompt_pool) * 2:
            logger.warning(
                "连续 %d 个 prompt 未产出（池已耗尽或全被跳过），提前结束于 %d/%d",
                consecutive_skips, produced, args.count,
            )
            break

        # ---- Phase 1: 收集 batch ----
        # --resume 跳过已见 prompt；--resume_keep 和非 resume 允许复用
        batch_items: list[tuple[dict, str]] = []
        scanned = 0
        while len(batch_items) < args.batch_size and produced + len(batch_items) < args.count:
            if scanned >= len(prompt_pool) * 2:
                break  # 扫了 2 圈池仍凑不齐 batch → 池已耗尽
            prompt_item = prompt_pool[pool_idx % len(prompt_pool)]
            pool_idx += 1
            scanned += 1
            prompt = prompt_item["prompt"]
            ph = _prompt_hash(prompt)
            # --resume 严格跳过已见；--resume_keep / 非 resume 允许复用
            if args.resume and ph in seen:
                consecutive_skips += 1
                continue
            batch_items.append((prompt_item, ph))

        if not batch_items:
            break  # 池已耗尽

        # ---- Phase 2: 候选生成 ----
        batch_prompts = [item[0]["prompt"] for item in batch_items]
        if use_batch:
            try:
                batch_candidates = generate_candidates_batch(
                    model, tokenizer, batch_prompts,
                    n=args.n_candidates, max_new_tokens=args.max_new_tokens,
                )
            except Exception as exc:
                logger.warning("batch 生成失败，降级逐条：%s", exc)
                batch_candidates = [
                    generate_candidates(model, tokenizer, p, n=args.n_candidates,
                                       max_new_tokens=args.max_new_tokens)
                    for p in batch_prompts
                ]
        else:
            batch_candidates = [
                generate_candidates(model, tokenizer, p, n=args.n_candidates,
                                   max_new_tokens=args.max_new_tokens)
                for p in batch_prompts
            ]

        # ---- Phase 3: 裁判 + 存盘（逐条，裁判仅 8 token 无需 batch）----
        batch_produced = 0
        for (prompt_item, ph), candidates in zip(batch_items, batch_candidates):
            if produced >= args.count:
                break
            prompt = prompt_item["prompt"]
            meta = prompt_item["meta"]

            # 候选相同则跳过（无偏好信号）
            if len(set(candidates)) < 2:
                skipped_same += 1
                consecutive_skips += 1
                continue

            user_text = _extract_user_text(prompt)

            # 评审 #5：候选顺序乱序后再呈交裁判——位置偏置与温度去相关
            shuffled = list(candidates)
            rng.shuffle(shuffled)

            verdict = judge.judge(user_text, shuffled[0], shuffled[1])

            if verdict == "A":
                chosen_text, rejected_text = shuffled[0], shuffled[1]
            elif verdict == "B":
                chosen_text, rejected_text = shuffled[1], shuffled[0]
            else:  # TIE
                skipped_tie += 1
                consecutive_skips += 1
                continue

            # 组装偏好对
            pair = {
                "prompt": prompt,
                "chosen": [{"role": "assistant", "content": chosen_text}],
                "rejected": [{"role": "assistant", "content": rejected_text}],
                "meta": {**meta, "source": "rlaif", "judge": args.judge},
            }

            buffer.append(pair)
            seen.add(ph)
            produced += 1
            batch_produced += 1
            consecutive_skips = 0

            # 存盘
            if len(buffer) >= args.checkpoint_every:
                for p in buffer:
                    append_pair(args.output, p)
                buffer.clear()
                logger.info("进度：%d/%d（跳过 相同=%d TIE=%d）[batch=%d]",
                            produced, args.count, skipped_same, skipped_tie, args.batch_size)

        # batch 内零产出 → consecutive_skips 已在 Phase 3 累计
        if batch_produced == 0 and not use_batch:
            pass  # 逐条模式已有 continue 逻辑

    # 5e. 写入剩余 buffer
    for p in buffer:
        append_pair(args.output, p)

    # ---- 6. 统计 ----
    logger.info("===== RLAIF 数据生成完成 =====")
    logger.info("文件: %s", args.output)
    logger.info("总数: %d 对", produced)
    logger.info("跳过: 候选相同=%d, 裁判TIE=%d", skipped_same, skipped_tie)
    logger.info("裁判: %s", args.judge)
    logger.info("下一步: python scripts/finetune/train_dpo.py --data %s "
                "--base_model %s --output_dir outputs/dpo-rlaif", args.output, args.policy_model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
