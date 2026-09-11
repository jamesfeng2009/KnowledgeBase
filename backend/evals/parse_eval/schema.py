"""解析评测集的真值标注模型与 manifest.jsonl 序列化。

真实语料接入方式：把客户 PDF/HTML 放入语料目录，并在 manifest.jsonl 中按本
schema 填写真值（表格结构、公式 LaTeX、参考文本、扫描标记）。合成 smoke 语料
由 smoke_corpus.py 生成并写入同一格式，保证"合成可跑、真实可替换"。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class CellSpan:
    """合并单元格区域 — 覆盖 (start_row, start_col, row_span, col_span)。"""

    row: int
    col: int
    row_span: int = 1
    col_span: int = 1


@dataclass
class TableTruth:
    """表格真值 — 结构级对照基准。

    cells[row][col] 存单元格文本；被合并区覆盖的后续单元格填空字符串。
    header_rows 标记双层表头占用的行数（用于检测解析器是否保留表头层级）。
    """

    rows: int
    cols: int
    cells: list[list[str]] = field(default_factory=list)
    spans: list[CellSpan] = field(default_factory=list)
    header_rows: int = 1


@dataclass
class ParseTruth:
    """单篇文档的解析真值。"""

    doc_id: str
    source: str            # 语料目录内相对路径
    kind: str              # pdf / html
    scan: bool = False     # 是否扫描件（走 OCR 路径）
    text: str = ""         # 参考纯文本（文本抽取 P·R；scan=True 时作为 OCR 真值）
    tables: list[TableTruth] = field(default_factory=list)
    formulas: list[str] = field(default_factory=list)   # 期望还原的 LaTeX 公式块


def table_to_dict(t: TableTruth) -> dict:
    return {
        "rows": t.rows,
        "cols": t.cols,
        "cells": t.cells,
        "spans": [
            {"row": s.row, "col": s.col, "row_span": s.row_span, "col_span": s.col_span}
            for s in t.spans
        ],
        "header_rows": t.header_rows,
    }


def truth_to_dict(t: ParseTruth) -> dict:
    return {
        "doc_id": t.doc_id,
        "source": t.source,
        "kind": t.kind,
        "scan": t.scan,
        "text": t.text,
        "tables": [table_to_dict(tb) for tb in t.tables],
        "formulas": t.formulas,
    }


def dict_to_truth(d: dict) -> ParseTruth:
    tables = []
    for td in d.get("tables", []):
        tables.append(
            TableTruth(
                rows=td["rows"],
                cols=td["cols"],
                cells=td.get("cells", []),
                spans=[
                    CellSpan(
                        row=s["row"],
                        col=s["col"],
                        row_span=s.get("row_span", 1),
                        col_span=s.get("col_span", 1),
                    )
                    for s in td.get("spans", [])
                ],
                header_rows=td.get("header_rows", 1),
            )
        )
    return ParseTruth(
        doc_id=d["doc_id"],
        source=d["source"],
        kind=d.get("kind", "pdf"),
        scan=d.get("scan", False),
        text=d.get("text", ""),
        tables=tables,
        formulas=d.get("formulas", []),
    )


def write_manifest(path: str, truths: list[ParseTruth]) -> None:
    """将真值列表写入 manifest.jsonl。"""
    with open(path, "w", encoding="utf-8") as fh:
        for t in truths:
            fh.write(json.dumps(truth_to_dict(t), ensure_ascii=False) + "\n")


def load_manifest(path: str) -> list[ParseTruth]:
    """读取 manifest.jsonl 为 ParseTruth 列表。"""
    truths: list[ParseTruth] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            truths.append(dict_to_truth(json.loads(line)))
    return truths