#!/usr/bin/env python
"""基线 vs 微调模型对比评测脚本 — 输出 Markdown 对比表格（可直接贴进面试材料）。

输入数据（与后端导出 pipeline 对齐的 golden.jsonl）：
    {"query":"...","expected_answer":"...","expected_doc_ids":[...],"meta":{...}}

两种评测模式（--mode）：
    retrieval  — 向量检索对比：sentence-transformers 分别加载 baseline / finetuned 向量模型，
                 对 golden 查询做全库检索，对比 Recall@5 / Recall@10 / MRR。
                 语料来源：--corpus 指定 {"doc_id","text"} JSONL；未指定时以 golden 的
                 expected_answer 构建伪语料（doc_id 取 expected_doc_ids[0]），适合快速演示。
    generation — 生成质量对比：transformers 分别加载 baseline / finetuned 生成模型
                 （--finetuned_adapter 可叠加 LoRA），对 query 贪心生成，
                 对比 avg BLEU / ROUGE-L / 关键词重叠率。

说明：
    - 本脚本默认纯离线运行，不依赖项目后端服务；若需复用后端 LLM 网关
      （app.llm.factory 的 provider 池 / 模型路由），可将 _generate_answers 中的
      transformers 推理替换为网关调用，指标计算部分无需改动。
    - faithfulness（忠实度）此处用"生成答案与参考答案的关键词重叠率"作为占位实现，
      生产环境建议替换为 NLI 模型判定或 LLM-as-judge（见 keyword_overlap 注释）。

依赖安装（独立 ML 工具链，不写入项目 requirements.txt）：
    pip install "torch>=2.2" "transformers>=4.45" "peft>=0.13" "sentence-transformers>=2.7"
    （指标计算与数据加载仅用标准库；仅模型加载需要上述依赖）

运行示例：
    # 检索对比（离线伪语料）
    python scripts/finetune/eval_compare.py --golden data/golden.jsonl --mode retrieval \
        --baseline_model BAAI/bge-base-zh-v1.5 --finetuned_model outputs/embedding-ft

    # 生成对比（基座 vs 基座+LoRA adapter）
    python scripts/finetune/eval_compare.py --golden data/golden.jsonl --mode generation \
        --baseline_model Qwen/Qwen2.5-7B-Instruct \
        --finetuned_model Qwen/Qwen2.5-7B-Instruct --finetuned_adapter outputs/lora-sft
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

logger = logging.getLogger("eval_compare")

_CJK_RE = re.compile(r"[一-鿿]")


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


def load_golden(path: str | Path) -> list[dict]:
    """加载 golden.jsonl，返回 [{"query","expected_answer","expected_doc_ids","meta"}]。

    校验规则：query 为非空字符串；expected_doc_ids 缺省视为空列表；
    expected_answer 缺省视为空串。可独立导入测试（仅依赖标准库）。
    """
    records: list[dict] = []
    for lineno, obj in iter_jsonl(path):
        query = obj.get("query")
        if not isinstance(query, str) or not query.strip():
            logger.warning("跳过样本 %s:%d — query 必须为非空字符串", path, lineno)
            continue
        expected_doc_ids = obj.get("expected_doc_ids")
        if not isinstance(expected_doc_ids, list):
            expected_doc_ids = []
        expected_answer = obj.get("expected_answer")
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        records.append({
            "query": query,
            "expected_answer": expected_answer if isinstance(expected_answer, str) else "",
            "expected_doc_ids": [str(d) for d in expected_doc_ids],
            "meta": meta,
        })
    logger.info("加载 golden 样本 %d 条 — %s", len(records), path)
    return records


def load_corpus(path: str | Path) -> tuple[list[str], list[str]]:
    """加载语料 JSONL（{"doc_id":"...","text":"..."}），返回 (doc_ids, texts)。"""
    doc_ids: list[str] = []
    texts: list[str] = []
    for lineno, obj in iter_jsonl(path):
        doc_id, text = obj.get("doc_id"), obj.get("text")
        if not isinstance(doc_id, str) or not isinstance(text, str) or not text.strip():
            logger.warning("跳过语料行 %s:%d — 需要非空 doc_id / text", path, lineno)
            continue
        doc_ids.append(doc_id)
        texts.append(text)
    logger.info("加载语料 %d 篇 — %s", len(doc_ids), path)
    return doc_ids, texts


def build_pseudo_corpus(golden: list[dict]) -> tuple[list[str], list[str]]:
    """从 golden 构建伪语料：doc_id 取 expected_doc_ids[0]，text 取 expected_answer。

    仅用于离线快速演示检索对比；正式评测请用 --corpus 提供真实文档库。
    """
    doc_ids: list[str] = []
    texts: list[str] = []
    seen: set[str] = set()
    for i, rec in enumerate(golden):
        if not rec["expected_answer"]:
            continue
        doc_id = rec["expected_doc_ids"][0] if rec["expected_doc_ids"] else f"golden_{i}"
        if doc_id in seen:
            continue
        seen.add(doc_id)
        doc_ids.append(doc_id)
        texts.append(rec["expected_answer"])
    return doc_ids, texts


# ---------------------------------------------------------------------------
# 纯标准库指标函数（可独立导入测试）
# ---------------------------------------------------------------------------

def recall_at_k(ranked_ids: Sequence[str], expected_ids: Iterable[str], k: int) -> float:
    """Recall@k：top-k 命中任一期望文档即记 1。expected_ids 为空时返回 0。"""
    expected = set(expected_ids)
    if not expected:
        return 0.0
    return 1.0 if expected & set(ranked_ids[:k]) else 0.0


def mrr(ranked_ids: Sequence[str], expected_ids: Iterable[str]) -> float:
    """MRR：首个命中期望文档的倒数排名；未命中返回 0。"""
    expected = set(expected_ids)
    if not expected:
        return 0.0
    for rank, doc_id in enumerate(ranked_ids, start=1):
        if doc_id in expected:
            return 1.0 / rank
    return 0.0


def _tokenize(text: str) -> list[str]:
    """双语分词：含 CJK 字符的文本按字切分（中文 BLEU/ROUGE 惯例），否则按空白分词。"""
    text = text.strip()
    if not text:
        return []
    if _CJK_RE.search(text):
        return [ch for ch in text if not ch.isspace()]
    return text.split()


def bleu_score(hypothesis: str, reference: str, max_n: int = 4) -> float:
    """句子级 BLEU（1-gram ~ max_n-gram，+1 平滑，带 brevity penalty）。

    中文按字切分，英文按词切分。返回 [0, 1]。任一文本为空返回 0。
    """
    hyp = _tokenize(hypothesis)
    ref = _tokenize(reference)
    if not hyp or not ref:
        return 0.0

    log_precisions = 0.0
    used_n = 0
    for n in range(1, max_n + 1):
        if len(hyp) < n or len(ref) < n:
            break
        hyp_ngrams = Counter(tuple(hyp[i:i + n]) for i in range(len(hyp) - n + 1))
        ref_ngrams = Counter(tuple(ref[i:i + n]) for i in range(len(ref) - n + 1))
        overlap = sum((hyp_ngrams & ref_ngrams).values())
        total = sum(hyp_ngrams.values())
        # +1 平滑，避免短句零精度直接归零
        log_precisions += math.log((overlap + 1) / (total + 1))
        used_n += 1
    if used_n == 0:
        return 0.0

    bp = 1.0 if len(hyp) > len(ref) else math.exp(1 - len(ref) / len(hyp))
    return bp * math.exp(log_precisions / used_n)


def rouge_l(hypothesis: str, reference: str) -> float:
    """ROUGE-L（LCS 的 F1）。中文按字、英文按词切分。任一文本为空返回 0。"""
    hyp = _tokenize(hypothesis)
    ref = _tokenize(reference)
    if not hyp or not ref:
        return 0.0

    # 动态规划求 LCS 长度
    prev = [0] * (len(ref) + 1)
    for i in range(1, len(hyp) + 1):
        curr = [0] * (len(ref) + 1)
        for j in range(1, len(ref) + 1):
            if hyp[i - 1] == ref[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    lcs = prev[-1]
    if lcs == 0:
        return 0.0
    precision = lcs / len(hyp)
    recall = lcs / len(ref)
    return 2 * precision * recall / (precision + recall)


def keyword_overlap(hypothesis: str, reference: str) -> float:
    """关键词重叠率：参考答案 token 集合中被生成答案覆盖的比例。

    作为 faithfulness（忠实度）的轻量占位实现 —— 衡量生成答案是否覆盖参考答案的
    关键信息点。生产环境建议替换为：
      a) NLI 模型逐句蕴含判定（如 roberta-large-mnli / 中文 nli 模型）；
      b) LLM-as-judge（用强模型按 rubric 打分，可复用后端 app.observability.llm_judge）。
    """
    hyp_tokens = set(_tokenize(hypothesis))
    ref_tokens = set(_tokenize(reference))
    if not ref_tokens:
        return 0.0
    return len(hyp_tokens & ref_tokens) / len(ref_tokens)


def format_markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """渲染 Markdown 表格（可直接贴进面试材料 / PR 描述）。"""
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "|" + "|".join(" --- " for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 检索模式（sentence-transformers，函数内延迟导入）
# ---------------------------------------------------------------------------

def eval_retrieval_model(model_name: str, golden: list[dict],
                         corpus_ids: list[str], corpus_texts: list[str],
                         ks: tuple[int, ...] = (5, 10)) -> dict[str, Any]:
    """单个向量模型在 golden 上的检索指标：Recall@k / MRR。"""
    import numpy as np  # 延迟导入
    from sentence_transformers import SentenceTransformer  # 延迟导入

    model = SentenceTransformer(model_name)
    q_emb = model.encode([r["query"] for r in golden],
                         batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    c_emb = model.encode(corpus_texts,
                         batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    sims = np.asarray(q_emb) @ np.asarray(c_emb).T
    max_k = max(min(k, len(corpus_ids)) for k in ks)
    order = np.argsort(-sims, axis=1)[:, :max_k]

    recalls = {k: 0.0 for k in ks}
    mrr_sum = 0.0
    for i, rec in enumerate(golden):
        ranked = [corpus_ids[j] for j in order[i]]
        for k in ks:
            recalls[k] += recall_at_k(ranked, rec["expected_doc_ids"], k)
        mrr_sum += mrr(ranked, rec["expected_doc_ids"])

    n = max(1, len(golden))
    result: dict[str, Any] = {"model": model_name, "samples": len(golden), "mrr": mrr_sum / n}
    for k in ks:
        result[f"recall@{k}"] = recalls[k] / n
    return result


# ---------------------------------------------------------------------------
# 生成模式（transformers，函数内延迟导入）
# ---------------------------------------------------------------------------

def _detect_bf16() -> bool:
    """检测当前设备是否支持 bf16（CUDA 或 Apple Silicon MPS）。

    用实际分配张量探测，比按版本号判断更可靠（MPS 自 PyTorch 2.3+/macOS 14+ 支持 bf16）。
    """
    import torch  # 延迟导入

    if torch.cuda.is_available():
        return bool(torch.cuda.is_bf16_supported())
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        try:
            torch.zeros(1, dtype=torch.bfloat16, device="mps")
            return True
        except Exception:
            return False
    return False


def _load_causal_lm(model_path: str, adapter_path: str | None = None):
    """加载生成模型；adapter_path 非空时叠加 LoRA adapter。"""
    import torch  # 延迟导入
    from transformers import AutoModelForCausalLM, AutoTokenizer  # 延迟导入

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    use_bf16 = _detect_bf16()
    has_accelerator = torch.cuda.is_available() or (
        getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available()
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16 if use_bf16 else torch.float32,
        device_map="auto" if has_accelerator else None,
        trust_remote_code=True,
    )
    if adapter_path:
        from peft import PeftModel  # 延迟导入
        model = PeftModel.from_pretrained(model, adapter_path)
    model.eval()
    return model, tokenizer


def _generate_answers(model_path: str, adapter_path: str | None, golden: list[dict],
                      max_new_tokens: int) -> list[str]:
    """对 golden 中的 query 逐条贪心生成答案。

    离线 transformers 实现；如需复用后端 LLM 网关，替换本函数为网关调用即可，
    上层指标计算不受影响。
    """
    import torch  # 延迟导入

    model, tokenizer = _load_causal_lm(model_path, adapter_path)
    answers: list[str] = []
    for rec in golden:
        messages = [{"role": "user", "content": rec["query"]}]
        if getattr(tokenizer, "chat_template", None):
            prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        else:
            prompt = f"<|user|>\n{rec['query']}\n<|assistant|>\n"
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            output = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )
        new_tokens = output[0][inputs["input_ids"].shape[1]:]
        answers.append(tokenizer.decode(new_tokens, skip_special_tokens=True).strip())
    # 主动释放显存，便于同进程加载下一个模型
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return answers


def eval_generation_model(model_path: str, adapter_path: str | None, golden: list[dict],
                          max_new_tokens: int = 512) -> dict[str, Any]:
    """单个生成模型在 golden 上的指标：avg BLEU / ROUGE-L / 关键词重叠率。"""
    answers = _generate_answers(model_path, adapter_path, golden, max_new_tokens)
    n = max(1, len(golden))
    bleu_sum = rouge_sum = overlap_sum = 0.0
    for rec, ans in zip(golden, answers):
        bleu_sum += bleu_score(ans, rec["expected_answer"])
        rouge_sum += rouge_l(ans, rec["expected_answer"])
        overlap_sum += keyword_overlap(ans, rec["expected_answer"])
    label = model_path if not adapter_path else f"{model_path}+{adapter_path}"
    return {
        "model": label,
        "samples": len(golden),
        "bleu": bleu_sum / n,
        "rouge_l": rouge_sum / n,
        "keyword_overlap": overlap_sum / n,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基线 vs 微调模型对比评测（输出 Markdown 表格）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--golden", required=True, help="golden.jsonl 路径")
    parser.add_argument("--mode", choices=["retrieval", "generation"], required=True, help="评测模式")
    parser.add_argument("--baseline_model", required=True,
                        help="基线模型（retrieval: 向量模型；generation: 生成模型）")
    parser.add_argument("--finetuned_model", required=True,
                        help="微调模型（generation 模式下为基座路径，配合 --finetuned_adapter）")
    parser.add_argument("--finetuned_adapter", default=None,
                        help="generation 模式可选：叠加在 finetuned_model 上的 LoRA adapter 路径")
    parser.add_argument("--corpus", default=None,
                        help="retrieval 模式可选：语料 JSONL（{\"doc_id\",\"text\"}）；缺省用 golden 构建伪语料")
    parser.add_argument("--limit", type=int, default=None, help="仅评测前 N 条（快速 smoke）")
    parser.add_argument("--max_new_tokens", type=int, default=512, help="generation 模式最大生成长度")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    golden = load_golden(args.golden)
    if args.limit:
        golden = golden[: args.limit]
    if not golden:
        logger.error("golden 数据集为空")
        return 1

    if args.mode == "retrieval":
        if args.corpus:
            corpus_ids, corpus_texts = load_corpus(args.corpus)
        else:
            logger.warning("未指定 --corpus，使用 golden.expected_answer 构建伪语料（仅供演示）")
            corpus_ids, corpus_texts = build_pseudo_corpus(golden)
        baseline = eval_retrieval_model(args.baseline_model, golden, corpus_ids, corpus_texts)
        finetuned = eval_retrieval_model(args.finetuned_model, golden, corpus_ids, corpus_texts)
        headers = ["模型", "Recall@5", "Recall@10", "MRR", "样本数"]
        rows = [
            [f"baseline: {baseline['model']}", f"{baseline['recall@5']:.4f}",
             f"{baseline['recall@10']:.4f}", f"{baseline['mrr']:.4f}", baseline["samples"]],
            [f"finetuned: {finetuned['model']}", f"{finetuned['recall@5']:.4f}",
             f"{finetuned['recall@10']:.4f}", f"{finetuned['mrr']:.4f}", finetuned["samples"]],
        ]
    else:
        baseline = eval_generation_model(args.baseline_model, None, golden, args.max_new_tokens)
        finetuned = eval_generation_model(args.finetuned_model, args.finetuned_adapter,
                                          golden, args.max_new_tokens)
        headers = ["模型", "avg BLEU", "ROUGE-L", "关键词重叠率(faithfulness占位)", "样本数"]
        rows = [
            [f"baseline: {baseline['model']}", f"{baseline['bleu']:.4f}",
             f"{baseline['rouge_l']:.4f}", f"{baseline['keyword_overlap']:.4f}", baseline["samples"]],
            [f"finetuned: {finetuned['model']}", f"{finetuned['bleu']:.4f}",
             f"{finetuned['rouge_l']:.4f}", f"{finetuned['keyword_overlap']:.4f}", finetuned["samples"]],
        ]

    table = format_markdown_table(headers, rows)
    print("\n对比评测结果（Markdown 表格，可直接贴入面试材料）:\n")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
