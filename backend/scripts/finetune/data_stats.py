#!/usr/bin/env python
"""数据集统计工具 — 微调数据画像分析。

读取任意符合本项目导出 pipeline 约定的 JSONL 数据文件（sft / dpo / embedding / golden），
打印样本数、文本长度统计（均值 / min / max / p50 / p90 / p99）、长度分桶分布、
meta.source 来源分布，便于在面试或评审中讲解数据画像。

支持的格式（自动嗅探）：
    - sft.jsonl       {"messages":[{"role":..., "content":...}, ...], "meta": {...}}
    - dpo.jsonl       {"prompt": "...", "chosen": "...", "rejected": "...", "meta": {...}}
    - embedding.jsonl {"query": "...", "pos": "...", "neg": "...", "meta": {...}}
    - golden.jsonl    {"query": "...", "expected_answer": "...", "expected_doc_ids": [...], "meta": {...}}

依赖：仅 Python 标准库（无需 GPU / torch）。

运行示例：
    python scripts/finetune/data_stats.py data/sft.jsonl
    python scripts/finetune/data_stats.py data/dpo.jsonl --json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger("data_stats")

#: 长度分桶边界（字符数），用于打印长度分布直方图
LENGTH_BINS = (64, 128, 256, 512, 1024, 2048, 4096)

MISSING_SOURCE = "<missing>"


def iter_jsonl(path: str | Path) -> Iterator[tuple[int, dict]]:
    """逐行读取 JSONL，跳过坏行（JSON 解析失败 / 非对象行）。

    Yields:
        (行号从 1 开始, 解析后的 dict)
    """
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


def detect_format(obj: dict) -> str:
    """根据字段嗅探样本格式：sft / dpo / embedding / golden / unknown。"""
    if isinstance(obj.get("messages"), list):
        return "sft"
    if "prompt" in obj and "chosen" in obj and "rejected" in obj:
        return "dpo"
    if "query" in obj and "pos" in obj and "neg" in obj:
        return "embedding"
    if "query" in obj and ("expected_answer" in obj or "expected_doc_ids" in obj):
        return "golden"
    return "unknown"


def extract_texts(obj: dict, fmt: str | None = None) -> list[str]:
    """抽取样本中所有文本字段（用于长度统计）。未知格式返回空列表。"""
    fmt = fmt or detect_format(obj)
    if fmt == "sft":
        return [
            m["content"]
            for m in obj.get("messages", [])
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        ]
    if fmt == "dpo":
        return [obj[k] for k in ("prompt", "chosen", "rejected") if isinstance(obj.get(k), str)]
    if fmt == "embedding":
        return [obj[k] for k in ("query", "pos", "neg") if isinstance(obj.get(k), str)]
    if fmt == "golden":
        return [obj[k] for k in ("query", "expected_answer") if isinstance(obj.get(k), str)]
    return []


def _percentile(sorted_vals: list[int], pct: float) -> int:
    """最近秩（nearest-rank）百分位数。sorted_vals 必须已升序排序且非空。"""
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_vals)))
    return sorted_vals[rank - 1]


def compute_length_stats(lengths: Iterable[int]) -> dict[str, Any]:
    """计算长度统计：count / mean / min / max / p50 / p90 / p99。"""
    vals = sorted(int(x) for x in lengths)
    if not vals:
        return {"count": 0, "mean": 0.0, "min": 0, "max": 0, "p50": 0, "p90": 0, "p99": 0}
    return {
        "count": len(vals),
        "mean": round(sum(vals) / len(vals), 2),
        "min": vals[0],
        "max": vals[-1],
        "p50": _percentile(vals, 50),
        "p90": _percentile(vals, 90),
        "p99": _percentile(vals, 99),
    }


def length_histogram(lengths: Iterable[int], bins: tuple[int, ...] = LENGTH_BINS) -> dict[str, int]:
    """按分桶统计长度分布，键形如 "<=64"、"65-128"、">4096"。"""
    hist: dict[str, int] = {}
    prev = 0
    keys: list[str] = []
    for b in bins:
        key = f"<={b}" if prev == 0 else f"{prev + 1}-{b}"
        keys.append(key)
        hist[key] = 0
        prev = b
    tail = f">{bins[-1]}"
    hist[tail] = 0
    for n in lengths:
        placed = False
        prev = 0
        for key, b in zip(keys, bins):
            if prev < n <= b:
                hist[key] += 1
                placed = True
                break
            prev = b
        if not placed:
            hist[tail] += 1
    return hist


def source_distribution(records: Iterable[dict]) -> dict[str, int]:
    """统计 meta.source 分布；缺失记为 <missing>。"""
    counter: Counter[str] = Counter()
    for rec in records:
        meta = rec.get("meta")
        source = meta.get("source") if isinstance(meta, dict) else None
        counter[str(source) if source else MISSING_SOURCE] += 1
    return dict(counter.most_common())


def summarize_file(path: str | Path) -> dict[str, Any]:
    """汇总一个 JSONL 文件的完整数据画像（可独立导入测试）。"""
    records: list[dict] = []
    lengths: list[int] = []
    format_counter: Counter[str] = Counter()
    total_lines = 0
    for _, obj in iter_jsonl(path):
        total_lines += 1
        fmt = detect_format(obj)
        format_counter[fmt] += 1
        records.append(obj)
        lengths.extend(len(t) for t in extract_texts(obj, fmt))
    return {
        "file": str(path),
        "samples": len(records),
        "format_counts": dict(format_counter.most_common()),
        "length_stats": compute_length_stats(lengths),
        "length_histogram": length_histogram(lengths),
        "source_distribution": source_distribution(records),
    }


def _print_report(summary: dict[str, Any]) -> None:
    """以人类可读格式打印数据画像。"""
    ls = summary["length_stats"]
    print(f"文件: {summary['file']}")
    print(f"样本数: {summary['samples']}")
    print("格式分布:")
    for fmt, cnt in summary["format_counts"].items():
        print(f"  - {fmt}: {cnt}")
    print(
        "文本长度(字符): "
        f"count={ls['count']} mean={ls['mean']} min={ls['min']} max={ls['max']} "
        f"p50={ls['p50']} p90={ls['p90']} p99={ls['p99']}"
    )
    print("长度分布:")
    for bucket, cnt in summary["length_histogram"].items():
        print(f"  {bucket:>10}: {cnt}")
    print("meta.source 分布:")
    for source, cnt in summary["source_distribution"].items():
        print(f"  - {source}: {cnt}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data", help="JSONL 数据文件路径（sft / dpo / embedding / golden 均可）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整统计结果")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if not Path(args.data).is_file():
        print(f"错误: 文件不存在 — {args.data}", file=sys.stderr)
        return 1

    summary = summarize_file(args.data)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
