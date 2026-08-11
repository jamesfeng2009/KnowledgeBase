#!/usr/bin/env python
"""Cross-encoder 精排（reranker）微调脚本 — sentence-transformers CrossEncoder。

输入数据：复用 embedding.jsonl（与后端导出 pipeline 对齐）：
    {"query":"...","pos":"...","neg":"...","meta":{...}}
    每条三元组展开为两个训练样本：(query, pos) -> label 1.0，(query, neg) -> label 0.0。

训练方式：
    - CrossEncoder(num_labels=1)，fit 默认损失为 BCEWithLogitsLoss（num_labels=1 时）
    - 留出 10% 数据做简单二分类准确率评估，训练前后各跑一次，展示微调收益

依赖安装（独立 ML 工具链，不写入项目 requirements.txt）：
    pip install "sentence-transformers>=2.7" "torch>=2.2"

运行示例：
    python scripts/finetune/train_reranker.py \
        --data data/embedding.jsonl --base_model BAAI/bge-reranker-base \
        --output_dir outputs/reranker-ft --epochs 2 --batch_size 16 --lr 1e-5
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("train_reranker")

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

    与 train_embedding.load_triplets 规则一致：query/pos/neg 均为非空字符串。
    可独立导入测试（仅依赖标准库）。
    """
    records: list[dict] = []
    for lineno, obj in iter_jsonl(path):
        query, pos, neg = obj.get("query"), obj.get("pos"), obj.get("neg")
        if not all(isinstance(x, str) and x.strip() for x in (query, pos, neg)):
            logger.warning("跳过样本 %s:%d — query/pos/neg 必须为非空字符串", path, lineno)
            continue
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        records.append({"query": query, "pos": pos, "neg": neg, "meta": meta})
    logger.info("加载三元组 %d 条 — %s", len(records), path)
    return records


def build_pairs(records: list[dict]) -> tuple[list[list[str]], list[float]]:
    """三元组 -> (query, passage, label) 二分类对：pos -> 1.0，neg -> 0.0。"""
    pairs: list[list[str]] = []
    labels: list[float] = []
    for r in records:
        pairs.append([r["query"], r["pos"]])
        labels.append(1.0)
        pairs.append([r["query"], r["neg"]])
        labels.append(0.0)
    return pairs, labels


def evaluate_accuracy(model: Any, pairs: list[list[str]], labels: list[float],
                      batch_size: int = 64, threshold: float = 0.5) -> float:
    """二分类准确率。num_labels=1 时 predict 默认经 Sigmoid，故阈值取 0.5。

    重依赖 numpy 在此函数内延迟导入。
    """
    import numpy as np  # 延迟导入

    if not pairs:
        return 0.0
    scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
    pred = (np.asarray(scores, dtype=float) >= threshold).astype(float)
    return float((pred == np.asarray(labels, dtype=float)).mean())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cross-encoder 精排微调（sentence-transformers CrossEncoder）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, help="embedding.jsonl 路径（复用三元组数据）")
    parser.add_argument("--base_model", default="BAAI/bge-reranker-base", help="基座精排模型")
    parser.add_argument("--output_dir", default="outputs/reranker-ft", help="微调模型输出目录")
    parser.add_argument("--epochs", type=int, default=2, help="训练轮数")
    parser.add_argument("--batch_size", type=int, default=16, help="batch size")
    parser.add_argument("--lr", type=float, default=1e-5, help="学习率")
    parser.add_argument("--max_len", type=int, default=512, help="query+passage 最大 token 长度")
    parser.add_argument("--eval_ratio", type=float, default=0.1, help="留出评估比例")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # ---- 重依赖延迟导入：保证无 GPU 环境（或无 sentence-transformers）下 import 本模块不报错 ----
    import torch
    from sentence_transformers import CrossEncoder, InputExample
    from torch.utils.data import DataLoader

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    records = load_triplets(args.data)

    # ---- 留出评估集（评审 #2：按基问题分组切分——三元组级切分只保证同一 query 的
    # 正负例不跨集合，但同基问题的 query 变体仍会跨集合泄漏，指标准确率虚高）----
    keys = [question_group_key(r["query"]) for r in records]
    n_eval = max(1, round(len(records) * args.eval_ratio))
    train_idx, eval_idx = grouped_split_indices(keys, test_size=n_eval, seed=args.seed)
    eval_records = [records[i] for i in eval_idx]
    train_records = [records[i] for i in train_idx]
    eval_pairs, eval_labels = build_pairs(eval_records)
    logger.info("训练三元组 %d 条 / 评估三元组 %d 条（评估对 %d 个）",
                len(train_records), len(eval_records), len(eval_pairs))

    model = CrossEncoder(args.base_model, num_labels=1, max_length=args.max_len)

    # ---- 训练前评估（基线准确率）----
    acc_before = evaluate_accuracy(model, eval_pairs, eval_labels, batch_size=args.batch_size)
    logger.info("[训练前] 二分类准确率 = %.4f", acc_before)

    # ---- 训练 ----
    train_pairs, train_labels = build_pairs(train_records)
    train_examples = [InputExample(texts=p, label=l) for p, l in zip(train_pairs, train_labels)]
    train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=args.batch_size)
    warmup_steps = max(1, int(len(train_dataloader) * args.epochs * 0.1))

    repro_info = {
        "base_model": args.base_model,
        "loss": "BCEWithLogitsLoss(num_labels=1, CrossEncoder.fit 默认)",
        "lr": args.lr,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "warmup_steps": warmup_steps,
        "max_len": args.max_len,
        "seed": args.seed,
        "num_train_pairs": len(train_examples),
        "num_eval_pairs": len(eval_pairs),
        "torch_version": torch.__version__,
    }
    logger.info("可复现信息:\n%s", json.dumps(repro_info, ensure_ascii=False, indent=2))

    model.fit(
        train_dataloader=train_dataloader,
        epochs=args.epochs,
        warmup_steps=warmup_steps,
        optimizer_params={"lr": args.lr},
        output_path=args.output_dir,
        show_progress_bar=True,
    )

    # ---- 训练后评估 ----
    acc_after = evaluate_accuracy(model, eval_pairs, eval_labels, batch_size=args.batch_size)
    logger.info("[训练后] 二分类准确率 = %.4f", acc_after)
    logger.info("微调收益: 准确率 %+.4f（%.4f -> %.4f）", acc_after - acc_before, acc_before, acc_after)
    logger.info("微调模型已保存至 %s", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
