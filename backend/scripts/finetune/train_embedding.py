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


def evaluate_recall(model: Any, samples: list[dict], k: int = 10, batch_size: int = 64) -> float:
    """轻量检索评估：以样本中全部去重 pos 为语料，计算 Recall@k（余弦相似度 top-k 命中）。

    用于训练前后对比，快速展示微调收益；非严格信息检索指标（语料小、无干扰文档）。
    重依赖 numpy 在此函数内延迟导入。
    """
    import numpy as np  # 延迟导入

    if not samples:
        return 0.0
    corpus = sorted({s["pos"] for s in samples})
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
    parser.add_argument("--eval_samples", type=int, default=100, help="训练前后 Recall@10 评估抽样条数")
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
    if len(records) < args.batch_size:
        logger.warning("样本数(%d) < batch_size(%d)，in-batch 负例过少会显著削弱 MNR 效果",
                       len(records), args.batch_size)

    model = SentenceTransformer(args.base_model)
    model.max_seq_length = args.max_len

    # ---- 训练前评估（基线 Recall@10）----
    rng = random.Random(args.seed)
    eval_samples = rng.sample(records, k=min(args.eval_samples, len(records)))
    recall_before = evaluate_recall(model, eval_samples, k=10, batch_size=args.batch_size)
    logger.info("[训练前] Recall@10 = %.4f（%d 条抽样）", recall_before, len(eval_samples))

    # ---- 训练 ----
    train_examples = [InputExample(texts=[r["query"], r["pos"], r["neg"]]) for r in records]
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
        "num_train_samples": len(records),
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

    # ---- 训练后评估（同一批抽样，对比微调收益）----
    recall_after = evaluate_recall(model, eval_samples, k=10, batch_size=args.batch_size)
    logger.info("[训练后] Recall@10 = %.4f（%d 条抽样）", recall_after, len(eval_samples))
    logger.info("微调收益: Recall@10 %+.4f（%.4f -> %.4f）",
                recall_after - recall_before, recall_before, recall_after)
    logger.info("微调模型已保存至 %s", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
