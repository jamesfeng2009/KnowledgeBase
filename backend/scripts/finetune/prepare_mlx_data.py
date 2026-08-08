#!/usr/bin/env python
"""MLX 数据准备脚本 — 将 sft.jsonl 转为 mlx-lm 训练目录格式。

mlx-lm（Apple 官方 LoRA 微调框架，M3 Max 上通常比 HF+MPS 快 2-3 倍）要求
数据以目录形式提供，目录内含 train.jsonl + valid.jsonl 两个文件，且每条样本
直接兼容本项目导出 pipeline 的 {"messages": [...]} chat 格式。

本脚本职责：
    1. 复用 train_lora.load_sft_jsonl 读取并校验 sft.jsonl（跳过坏行 / 不合法样本）
    2. 按比例随机打乱后 9:1 拆分为 train / valid（valid 不足时回退留 1 条保证不为空）
    3. 写入 <output_dir>/train.jsonl + <output_dir>/valid.jsonl（ensure_ascii=False）
    4. 打印拆分统计（样本数 / 拆分比例 / 各文件路径），便于核对

依赖：仅 Python 标准库（无需 GPU / torch / mlx）。运行示例：

    # 1. 导出 SFT 数据集（后端管理页或 API 触发构建）
    # 2. 转为 MLX 目录
    python scripts/finetune/prepare_mlx_data.py \
        --data data/finetune/t1/v20260808-1000/sft.jsonl \
        --output_dir data/mlx/qwen-sft

    # 3. mlx-lm 微调（需先 pip install mlx-lm 到独立 venv，勿混入项目 .venv）
    mlx_lm.lora --model Qwen/Qwen2.5-7B-Instruct-4bit \
        --train --data data/mlx/qwen-sft \
        --iters 600 --batch-size 4 --lora-layers 16 \
        --adapter-path outputs/mlx-lora
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

logger = logging.getLogger("prepare_mlx_data")

#: 复用 train_lora 的 SFT 加载器（保持与 HF 链路校验逻辑一致，避免重复实现）
_SCRIPTS_DIR = Path(__file__).resolve().parent


def _import_loader():
    """按文件路径加载 train_lora 模块（scripts/finetune 非 Python 包，不经 sys.path）。

    返回模块对象，取其 load_sft_jsonl。延迟到函数内执行，避免 import 本模块时
    强制解析 train_lora（其顶层仅有标准库，但保持隔离更稳健）。
    """
    import importlib.util

    path = _SCRIPTS_DIR / "train_lora.py"
    spec = importlib.util.spec_from_file_location("_finetune_train_lora", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_dataset(
    records: list[dict],
    valid_ratio: float = 0.1,
    seed: int = 42,
    min_valid: int = 1,
) -> tuple[list[dict], list[dict]]:
    """随机打乱后按比例拆分 train / valid。

    Args:
        records: 已校验的 SFT 样本列表
        valid_ratio: 验证集比例（0~1），默认 0.1
        seed: 随机种子，保证可复现
        min_valid: 验证集最少样本数（不足时从 train 借调，保证 valid 不为空）

    Returns:
        (train_records, valid_records)
    """
    if not records:
        return [], []
    rng = random.Random(seed)
    shuffled = list(records)
    rng.shuffle(shuffled)

    valid_count = max(min_valid, int(len(shuffled) * valid_ratio))
    valid_count = min(valid_count, len(shuffled) - 1) if len(shuffled) > 1 else min(valid_count, len(shuffled))
    valid = shuffled[:valid_count]
    train = shuffled[valid_count:]
    return train, valid


def write_jsonl(records: list[dict], path: Path) -> int:
    """写入 JSONL 文件（ensure_ascii=False，每行一个 JSON 对象）。返回写入条数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            # 保留 messages + meta，mlx-lm 直接消费 {"messages": [...]} 格式
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")
    return len(records)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 sft.jsonl 转为 mlx-lm 训练目录（train.jsonl + valid.jsonl）"
    )
    parser.add_argument("--data", required=True, help="输入 sft.jsonl 路径（后端导出 pipeline 产物）")
    parser.add_argument(
        "--output_dir",
        required=True,
        help="输出目录（train.jsonl / valid.jsonl 将写入此目录）",
    )
    parser.add_argument("--valid_ratio", type=float, default=0.1, help="验证集比例（默认 0.1）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    train_lora = _import_loader()
    records = train_lora.load_sft_jsonl(args.data)
    if not records:
        logger.error("无可用 SFT 样本，退出（检查输入文件：%s）", args.data)
        return 1

    train, valid = split_dataset(records, valid_ratio=args.valid_ratio, seed=args.seed)
    if not train:
        logger.error("拆分后训练集为空（总样本 %d / valid_ratio %.2f），降低 valid_ratio 重试", len(records), args.valid_ratio)
        return 1

    out_dir = Path(args.output_dir)
    train_path = out_dir / "train.jsonl"
    valid_path = out_dir / "valid.jsonl"
    n_train = write_jsonl(train, train_path)
    n_valid = write_jsonl(valid, valid_path)

    logger.info("MLX 数据准备完成：")
    logger.info("  源文件:        %s（校验通过 %d 条）", args.data, len(records))
    logger.info("  训练集:        %s（%d 条，占 %.1f%%）", train_path, n_train, 100.0 * n_train / len(records))
    logger.info("  验证集:        %s（%d 条，占 %.1f%%）", valid_path, n_valid, 100.0 * n_valid / len(records))
    logger.info("  valid_ratio=%.2f  seed=%d", args.valid_ratio, args.seed)
    logger.info("下一步：mlx_lm.lora --model Qwen/Qwen2.5-7B-Instruct-4bit --train --data %s ...", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
