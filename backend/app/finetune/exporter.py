"""
微调数据集导出器 — 单一职责：JSONL 落盘与版本目录管理。

目录布局::

    backend/data/finetune/{tenant_id}/{version}/{dataset_type}.jsonl

- version 格式：v{YYYYMMDD-HHmmss}（构建时刻，秒级）；
- tenant_id 为 None 时归入 "default" 目录（单租户兜底，与推荐模块一致）；
- 幂等性：同 version 重复导出为覆盖写，结果一致，可安全重试。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

#: backend/ 根目录（app/finetune/exporter.py → 上三级）
_BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

#: 默认导出根目录
DEFAULT_EXPORT_BASE_DIR: Path = _BACKEND_ROOT / "data" / "finetune"


def make_version(now: datetime | None = None) -> str:
    """生成版本号 — v{YYYYMMDD-HHmmss}。"""
    return f"v{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}"


def export_jsonl(
    samples: list[dict[str, Any]],
    tenant_id: str | None,
    dataset_type: str,
    version: str | None = None,
    *,
    base_dir: Path | None = None,
) -> tuple[Path, int]:
    """将样本写入 JSONL 文件。

    Args:
        samples: 样本 dict 列表（每行一个 JSON）。
        tenant_id: 租户 ID 字符串；None 归入 "default"。
        dataset_type: 数据集类型（sft/dpo/embedding/golden）。
        version: 版本号；None 时按当前时间生成。
        base_dir: 导出根目录（测试可注入临时目录）。

    Returns:
        (文件路径, 文件字节数)。
    """
    version = version or make_version()
    out_dir = (base_dir or DEFAULT_EXPORT_BASE_DIR) / (tenant_id or "default") / version
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"{dataset_type}.jsonl"

    with file_path.open("w", encoding="utf-8") as fp:
        for sample in samples:
            fp.write(json.dumps(sample, ensure_ascii=False) + "\n")

    return file_path, file_path.stat().st_size


def read_jsonl_head(file_path: Path, limit: int = 5) -> list[dict[str, Any]]:
    """读取 JSONL 前 N 行（preview 端点用）。

    空行跳过；坏行不静默吞掉——JSONL 由本模块写出，格式可信。
    """
    items: list[dict[str, Any]] = []
    with Path(file_path).open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
            if len(items) >= limit:
                break
    return items
