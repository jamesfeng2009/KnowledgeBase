"""
离线评测数据集 — 单一职责：定义评测用例与数据集加载。

设计要点：
    - 使用 dataclass（与 llm_judge.py 的 EvalResult 保持一致风格）；
    - JSONL 格式存储，每行一个 JSON 对象，便于版本管理与增量追加；
    - 支持单文件加载与目录批量加载；
    - 加载失败时优雅降级（跳过坏行 / 空文件返回空数据集），不抛异常中断流程。

JSONL 单行格式示例::

    {"query": "报销流程是什么", "expected_doc_ids": ["doc_1", "doc_2"],
     "expected_answer": "请先填写报销单...", "kb_ids": ["kb_1"], "tags": ["报销", "财务"]}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class EvalCase:
    """单条评测用例。

    Attributes:
        query: 用户问题。
        expected_doc_ids: 期望命中的文档 ID 列表（用于检索指标计算）。
        expected_answer: 期望答案文本（可选，用于人工比对或答案匹配）。
        kb_ids: 限定检索的知识库 ID 列表（可选，覆盖运行级 kb_ids）。
        tags: 用例标签（便于按维度筛选统计，如 ["报销", "财务"]）。
    """

    query: str
    expected_doc_ids: list[str] = field(default_factory=list)
    expected_answer: str | None = None
    kb_ids: list[str] | None = None
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        """从字典构造 EvalCase，容忍缺失字段与类型不规范。

        Args:
            data: 原始字典（通常由 JSON.loads 得来）。

        Returns:
            EvalCase 实例。query 缺失时返回空 query（由调用方决定是否过滤）。
        """
        query = str(data.get("query", "")).strip()
        expected_doc_ids = [
            str(d) for d in data.get("expected_doc_ids", []) if d is not None
        ]
        expected_answer = data.get("expected_answer")
        if expected_answer is not None:
            expected_answer = str(expected_answer)

        kb_ids_raw = data.get("kb_ids")
        kb_ids: list[str] | None = None
        if kb_ids_raw is not None:
            kb_ids = [str(k) for k in kb_ids_raw if k is not None]

        tags = [str(t) for t in data.get("tags", []) if t is not None]

        return cls(
            query=query,
            expected_doc_ids=expected_doc_ids,
            expected_answer=expected_answer,
            kb_ids=kb_ids,
            tags=tags,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "query": self.query,
            "expected_doc_ids": self.expected_doc_ids,
            "expected_answer": self.expected_answer,
            "kb_ids": self.kb_ids,
            "tags": self.tags,
        }


class EvalDataset:
    """评测数据集 — 持有一组 EvalCase，支持从 JSONL 加载。

    使用方式::

        ds = EvalDataset.load("eval_datasets/sample.jsonl")
        for case in ds:
            ...
        print(len(ds))
    """

    def __init__(self, cases: list[EvalCase] | None = None) -> None:
        self._cases: list[EvalCase] = list(cases) if cases else []

    # ------------------------------------------------------------------
    # 属性与协议
    # ------------------------------------------------------------------

    @property
    def cases(self) -> list[EvalCase]:
        """返回全部评测用例列表。"""
        return self._cases

    def __len__(self) -> int:
        return len(self._cases)

    def __iter__(self) -> Iterator[EvalCase]:
        return iter(self._cases)

    def __bool__(self) -> bool:
        return len(self._cases) > 0

    # ------------------------------------------------------------------
    # 加载器
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> EvalDataset:
        """从单个 JSONL 文件加载评测数据集。

        逐行解析，跳过空行与解析失败的坏行（记录警告），保证一个坏行不中断整体加载。

        Args:
            path: JSONL 文件路径。

        Returns:
            EvalDataset 实例（文件不存在或为空时返回空数据集）。
        """
        cases: list[EvalCase] = []
        if not path or not os.path.isfile(path):
            log.warning("eval_dataset.file_not_found", path=path)
            return cls(cases)

        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line_no, raw_line in enumerate(fh, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        log.warning(
                            "eval_dataset.line_parse_error",
                            path=path,
                            line=line_no,
                            error=str(exc),
                        )
                        continue
                    if not isinstance(obj, dict):
                        log.warning(
                            "eval_dataset.line_not_object",
                            path=path,
                            line=line_no,
                        )
                        continue
                    case = EvalCase.from_dict(obj)
                    # 过滤掉空 query 的用例（无意义）
                    if not case.query:
                        log.warning(
                            "eval_dataset.empty_query_skipped",
                            path=path,
                            line=line_no,
                        )
                        continue
                    cases.append(case)
        except OSError as exc:
            log.error("eval_dataset.read_error", path=path, error=str(exc))
            return cls(cases)

        log.info("eval_dataset.loaded", path=path, count=len(cases))
        return cls(cases)

    @classmethod
    def load_from_dir(cls, dir_path: str) -> EvalDataset:
        """从目录加载所有 .jsonl 文件，合并为一个数据集。

        按文件名排序后依次加载，保证加载顺序稳定。

        Args:
            dir_path: 目录路径。

        Returns:
            合并后的 EvalDataset（目录不存在时返回空数据集）。
        """
        if not dir_path or not os.path.isdir(dir_path):
            log.warning("eval_dataset.dir_not_found", dir=dir_path)
            return cls([])

        merged: list[EvalCase] = []
        jsonl_files = sorted(
            f for f in os.listdir(dir_path) if f.endswith(".jsonl")
        )
        for fname in jsonl_files:
            full = os.path.join(dir_path, fname)
            ds = cls.load(full)
            merged.extend(ds.cases)

        log.info(
            "eval_dataset.dir_loaded",
            dir=dir_path,
            file_count=len(jsonl_files),
            total=len(merged),
        )
        return cls(merged)
