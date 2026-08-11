#!/usr/bin/env python
"""Embedding 对比学习微调脚本 — 检索向量模型领域适配（sentence-transformers）。

输入数据（与后端导出 pipeline 对齐的 embedding.jsonl）：
    {"query":"...","pos":"...","neg":"...","meta":{...}}
    query 为用户问题，pos 为正例文档片段，neg 为（难）负例文档片段。

训练方式：
    - MultipleNegativesRankingLoss（InfoNCE）：(query, pos, neg) 三元组天然匹配 ——
      anchor 对 batch 内全部候选（正例 + 显式难负例 + 其余样本的 in-batch 负例）做对比
    - 训练前 / 训练后各跑一次轻量检索评估：从数据中抽样（默认 100 条），
      以全体 pos 为语料计算 Recall@10，直接展示微调收益

依赖安装（独立 ML 工具链，不写入项目 requirements.txt）：
    pip install "sentence-transformers>=2.7" "torch>=2.2"

运行示例：
    python scripts/finetune/train_embedding.py \
        --data data/embedding.jsonl --base_model BAAI/bge-base-zh-v1.5 \
        --output_dir outputs/embedding-ft --epochs 3 --batch_size 32 --lr 2e-5
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("train_embedding")

# 分组切分工具（评审 #2：同一基问题的 query 变体不得跨 train/eval）
from finetune_utils import grouped_split_indices, question_group_key


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


def load_triplets(path: str | Path) -> list[dict]:
    """加载 embedding.jsonl，返回 [{"query":..., "pos":..., "neg":..., "meta":{...}}]。

    校验规则：query/pos/neg 均为非空字符串。可独立导入测试（仅依赖标准库）。
    """
    records: list[dict] = []
    for lineno, obj in iter_jsonl(path):
        query, pos, neg = obj.get("query"), obj.get("pos"), obj.get("neg")
        if not all(isinstance(x, str) and x.strip() for x in (query, pos, neg)):
            logger.warning("跳过样本 %s:%d — query/pos/neg 必须为非空字符串", path, lineno)
            continue
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        records.append({"query": query, "pos": pos, "neg": neg, "meta": meta})
    logger.info("加载 embedding 三元组 %d 条 — %s", len(records), path)
    return records


def evaluate_recall(
    model: Any,
    samples: list[dict],
    k: int = 10,
    batch_size: int = 64,
    distractors: list[str] | None = None,
) -> float:
    """轻量检索评估：以 pos + 干扰文档为语料，计算 Recall@k（余弦相似度 top-k 命中）。

    语料构成：
    - 全部去重 pos（正确答案）；
    - 干扰文档（distractors，非匹配文档片段），模拟真实检索场景中的干扰项。

    干扰文档拉低基线 Recall@10（如从 1.0 降至 0.6-0.8），留出微调提升空间，
    使指标能反映真实的微调收益。无 distractors 时退化为原逻辑（仅 pos 语料）。
    重依赖 numpy 在此函数内延迟导入。
    """
    import numpy as np  # 延迟导入

    if not samples:
        return 0.0
    corpus_pos = sorted({s["pos"] for s in samples})
    # 构建语料：pos + 干扰文档（去重）
    corpus = list(corpus_pos)
    if distractors:
        existing = set(corpus)
        for d in distractors:
            if isinstance(d, str) and d.strip() and d not in existing:
                corpus.append(d)
                existing.add(d)
    pos_index = {text: idx for idx, text in enumerate(corpus)}
    k_eff = max(1, min(k, len(corpus)))

    q_emb = model.encode(
        [s["query"] for s in samples],
        batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False,
    )
    c_emb = model.encode(
        corpus, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False,
    )
    sims = np.asarray(q_emb) @ np.asarray(c_emb).T
    topk = np.argsort(-sims, axis=1)[:, :k_eff]

    hits = sum(1 for i, s in enumerate(samples) if pos_index[s["pos"]] in set(topk[i].tolist()))
    return hits / len(samples)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Embedding 对比学习微调（sentence-transformers + MultipleNegativesRankingLoss）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, help="embedding.jsonl 路径")
    parser.add_argument("--base_model", default="BAAI/bge-base-zh-v1.5", help="基座向量模型")
    parser.add_argument("--output_dir", default="outputs/embedding-ft", help="微调模型输出目录")
    parser.add_argument("--epochs", type=int, default=3, help="训练轮数")
    parser.add_argument("--batch_size", type=int, default=32, help="batch size（MNR 依赖 in-batch 负例，不宜过小）")
    parser.add_argument("--lr", type=float, default=2e-5, help="学习率")
    parser.add_argument("--max_len", type=int, default=512, help="最大 token 长度")
    parser.add_argument("--eval_samples", type=int, default=100, help="评测集条数上限（从数据尾部留出，不参与训练）")
    parser.add_argument("--eval_ratio", type=float, default=0.2, help="评测集占比（与 --eval_samples 取较小者）")
    parser.add_argument("--num_distractors", type=int, default=200, help="干扰文档数量（0=不加干扰，仅 pos 语料；>0 从非匹配 pos+neg 中抽取）")
    parser.add_argument("--eval_k", type=int, default=1, help="Recall@k 的 k 值（1=最严格，10=宽松；小语料建议 1）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # ---- 重依赖延迟导入：保证无 GPU 环境（或无 sentence-transformers）下 import 本模块不报错 ----
    import torch
    from sentence_transformers import InputExample, SentenceTransformer, losses
    from torch.utils.data import DataLoader

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    records = load_triplets(args.data)

    model = SentenceTransformer(args.base_model)
    model.max_seq_length = args.max_len

    # ---- 数据拆分：train / eval（eval 不参与训练，独立评测泛化能力）----
    # 评审 #2：按基问题分组切分 —— 生成侧每基问题产 8-10 个 query 变体，
    # 行级随机切分会让变体同时落入 train/eval，Recall 虚高。
    keys = [question_group_key(r["query"]) for r in records]
    eval_size = max(1, min(args.eval_samples, int(len(records) * args.eval_ratio)))
    train_idx, eval_idx = grouped_split_indices(keys, test_size=eval_size, seed=args.seed)
    eval_records = [records[i] for i in eval_idx]
    train_records = [records[i] for i in train_idx]
    rng = random.Random(args.seed)

    # ---- 干扰文档：从全部 pos + neg 中抽取非 eval 的 pos（模拟真实检索的干扰项）----
    # neg 也是文档片段，加入干扰池更贴近真实检索场景，拉低基线 Recall 留出提升空间
    all_docs = list({r["pos"] for r in records} | {r["neg"] for r in records})
    eval_pos_set = {s["pos"] for s in eval_records}
    distractor_pool = [d for d in all_docs if d not in eval_pos_set]
    if args.num_distractors > 0:
        if len(distractor_pool) > args.num_distractors:
            rng.shuffle(distractor_pool)
            distractor_pool = distractor_pool[:args.num_distractors]
        distractors: list[str] | None = distractor_pool
    else:
        distractors = None

    corpus_size = len({s["pos"] for s in eval_records}) + len(distractors or [])
    logger.info("数据拆分: train=%d, eval=%d, 干扰文档=%d, 语料总量=%d, Recall@%d",
                len(train_records), len(eval_records), len(distractors or []), corpus_size, args.eval_k)

    # ---- 训练前评估（基线 Recall@k，独立 eval 集 + 干扰文档）----
    recall_before = evaluate_recall(
        model, eval_records, k=args.eval_k, batch_size=args.batch_size, distractors=distractors,
    )
    logger.info("[训练前] Recall@%d = %.4f（eval=%d, 语料=%d）",
                args.eval_k, recall_before, len(eval_records), corpus_size)

    # ---- 训练（只用 train_records，eval 不参与）----
    if len(train_records) < args.batch_size:
        logger.warning("训练样本数(%d) < batch_size(%d)，in-batch 负例过少会显著削弱 MNR 效果",
                       len(train_records), args.batch_size)
    train_examples = [InputExample(texts=[r["query"], r["pos"], r["neg"]]) for r in train_records]
    train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=args.batch_size, drop_last=True)
    train_loss = losses.MultipleNegativesRankingLoss(model)
    warmup_steps = max(1, int(len(train_dataloader) * args.epochs * 0.1))

    repro_info = {
        "base_model": args.base_model,
        "loss": "MultipleNegativesRankingLoss(anchor, pos, neg)",
        "lr": args.lr,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "warmup_steps": warmup_steps,
        "max_len": args.max_len,
        "seed": args.seed,
        "num_train_samples": len(train_records),
        "num_eval_samples": len(eval_records),
        "num_distractors": len(distractors or []),
        "torch_version": torch.__version__,
    }
    logger.info("可复现信息:\n%s", json.dumps(repro_info, ensure_ascii=False, indent=2))

    model.fit(
        train_objectives=[(train_dataloader, train_loss)],
        epochs=args.epochs,
        warmup_steps=warmup_steps,
        optimizer_params={"lr": args.lr},
        output_path=args.output_dir,
        show_progress_bar=True,
    )

    # ---- 训练后评估（同一批 eval + 干扰文档，对比微调收益）----
    recall_after = evaluate_recall(
        model, eval_records, k=args.eval_k, batch_size=args.batch_size, distractors=distractors,
    )
    logger.info("[训练后] Recall@%d = %.4f（eval=%d, 语料=%d）",
                args.eval_k, recall_after, len(eval_records), corpus_size)
    logger.info("微调收益: Recall@%d %+.4f（%.4f -> %.4f）",
                args.eval_k, recall_after - recall_before, recall_before, recall_after)
    logger.info("微调模型已保存至 %s", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
