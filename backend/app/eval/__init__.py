"""
离线评测系统 — 提供数据集加载、评测 Runner、回归基线持久化能力。

导出：
    - EvalCase / EvalDataset：评测数据集与用例定义；
    - EvalRunner / EvalRunResult：评测执行器与汇总结果；
    - EvalRepository：评测结果持久化与回归基线管理。

设计要点：
    - 与在线评测（app.observability.llm_judge.LLMJudgeService）互补：
      在线评测面向单条实时问答，离线评测面向批量数据集回归；
    - 检索层指标（Recall@K / MRR / NDCG）为纯数学，不依赖 LLM；
    - 所有外部依赖不可用时优雅降级。
"""

from __future__ import annotations

from app.eval.dataset import EvalCase, EvalDataset
from app.eval.ragas_metrics import RagasMetrics
from app.eval.repository import EvalRepository, EvalResultRecord
from app.eval.runner import (
    EvalCaseResult,
    EvalRunResult,
    EvalRunner,
    recall_at_k,
    mrr,
    ndcg,
)
from app.eval.unified_metrics import MetricsAdapter, UnifiedMetrics

__all__ = [
    "EvalCase",
    "EvalDataset",
    "EvalRunner",
    "EvalRunResult",
    "EvalCaseResult",
    "EvalRepository",
    "EvalResultRecord",
    "RagasMetrics",
    "MetricsAdapter",
    "UnifiedMetrics",
    "recall_at_k",
    "mrr",
    "ndcg",
]
