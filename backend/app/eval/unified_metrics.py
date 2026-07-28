"""
统一评测指标适配器 — 消除在线 RagEvalService 与离线 EvalRunner 之间的指标割裂。

两套评测体系的指标对照：

| 维度         | 在线 RagEvalService            | 离线 EvalRunner                  | RAGAS 标准         |
|-------------|-------------------------------|----------------------------------|--------------------|
| 检索召回     | recall_at_1/3/5               | recall_at_k (K=5)               | -                  |
| 排序质量     | mrr, ndcg_at_5, map           | mrr, ndcg (K=5)                 | -                  |
| 引用准确性   | -（仅检索层）                   | judge.citation_accuracy (0-5)    | context_precision  |
| 完整性       | -（仅检索层）                   | judge.completeness (0-5)         | context_recall     |
| 忠实度       | -（仅检索层）                   | judge.hallucination_inverse (0-5)| faithfulness       |
| 切题度       | -（仅检索层）                   | -（无对应维度）                   | answer_relevancy   |

本模块提供：
    1. UnifiedMetrics：统一指标数据类，合并检索层 + 生成层 + RAGAS 指标
    2. MetricsAdapter：适配器，将 LLMJudge 0-5 分映射到 RAGAS 0-1 分
    3. unify_results()：将在线/离线结果统一为 UnifiedMetrics

使用方式::

    from app.eval.unified_metrics import MetricsAdapter

    adapter = MetricsAdapter()
    # Judge 分转 RAGAS 分
    ragas_like = adapter.judge_to_ragas(citation_accuracy=4, completeness=5, hallucination_inverse=4)
    # ragas_like = {"context_precision": 0.8, "context_recall": 1.0, "faithfulness": 0.8}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

#: Judge 满分（0-5 分制）
_JUDGE_MAX = 5

#: RAGAS 满分（0-1 分制）
_RAGAS_MAX = 1.0

#: 统一指标名称
UNIFIED_KEYS = (
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
    "judge_total_score",
)


@dataclass
class UnifiedMetrics:
    """统一评测指标 — 合并检索层 + 生成层 + RAGAS 指标。

    所有指标取值 0.0 ~ 1.0（judge_total_score 保持 0-5 原始分）。
    缺失的维度为 None，不参与均值计算。
    """

    # 检索层指标
    recall_at_5: float | None = None
    mrr: float | None = None
    ndcg_at_5: float | None = None

    # 生成层指标（RAGAS 标准 0-1 分）
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    # Judge 原始分（0-5 分制，保持不变便于横向对比）
    judge_total_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为字典，None 值保留为 None。"""
        return {
            "recall_at_5": self.recall_at_5,
            "mrr": self.mrr,
            "ndcg_at_5": self.ndcg_at_5,
            "faithfulness": self.faithfulness,
            "answer_relevancy": self.answer_relevancy,
            "context_precision": self.context_precision,
            "context_recall": self.context_recall,
            "judge_total_score": self.judge_total_score,
        }

    @property
    def generation_score(self) -> float | None:
        """生成层综合分（RAGAS 四项均值），无数据时返回 None。"""
        values = [
            v for v in (
                self.faithfulness,
                self.answer_relevancy,
                self.context_precision,
                self.context_recall,
            ) if v is not None
        ]
        return sum(values) / len(values) if values else None


class MetricsAdapter:
    """评测指标适配器 — 在 LLMJudge 0-5 分与 RAGAS 0-1 分之间转换。

    维度映射关系：
        - judge.citation_accuracy (0-5)  ↔ RAGAS.context_precision (0-1)
        - judge.completeness (0-5)       ↔ RAGAS.context_recall (0-1)
        - judge.hallucination_inverse (0-5) ↔ RAGAS.faithfulness (0-1)
        - judge.answer_relevancy (无)     → RAGAS.answer_relevancy (无 Judge 对应)
    """

    @staticmethod
    def judge_to_ragas(
        citation_accuracy: int | float = 0,
        completeness: int | float = 0,
        hallucination_inverse: int | float = 0,
    ) -> dict[str, float]:
        """将 LLMJudge 三维评分（0-5）映射为 RAGAS 三维评分（0-1）。

        Args:
            citation_accuracy: 引用准确性（0-5）。
            completeness: 完整性（0-5）。
            hallucination_inverse: 无幻觉度（0-5）。

        Returns:
            RAGAS 格式指标字典。
        """
        return {
            "context_precision": _scale_to_ragas(citation_accuracy),
            "context_recall": _scale_to_ragas(completeness),
            "faithfulness": _scale_to_ragas(hallucination_inverse),
        }

    @staticmethod
    def ragas_to_judge(
        faithfulness: float = 0.0,
        answer_relevancy: float = 0.0,
        context_precision: float = 0.0,
        context_recall: float = 0.0,
    ) -> dict[str, float]:
        """将 RAGAS 四维评分（0-1）映射为 LLMJudge 三维评分（0-5）。

        answer_relevancy 在 Judge 体系中无对应维度，映射到 completeness 的加权。

        Returns:
            Judge 格式指标字典 + total_score。
        """
        citation_accuracy = _scale_to_judge(context_precision)
        hallucination_inverse = _scale_to_judge(faithfulness)
        # completeness = context_recall * 0.7 + answer_relevancy * 0.3
        completeness = _scale_to_judge(
            context_recall * 0.7 + answer_relevancy * 0.3
        )
        total_score = (citation_accuracy + completeness + hallucination_inverse) / 3.0
        return {
            "citation_accuracy": citation_accuracy,
            "completeness": completeness,
            "hallucination_inverse": hallucination_inverse,
            "total_score": round(total_score, 2),
        }

    @staticmethod
    def unify_case_result(
        retrieval_metrics: dict[str, Any] | None = None,
        judge_scores: dict[str, Any] | None = None,
        ragas_scores: dict[str, float] | None = None,
    ) -> UnifiedMetrics:
        """将多个来源的指标合并为统一格式。

        优先级：RAGAS > Judge > 检索（同一维度有多个来源时取优先级高的）。

        Args:
            retrieval_metrics: 检索层指标（recall_at_5, mrr, ndcg_at_5 等）。
            judge_scores: LLMJudge 评分（citation_accuracy, completeness 等）。
            ragas_scores: RAGAS 标准指标（faithfulness 等）。

        Returns:
            UnifiedMetrics 实例。
        """
        result = UnifiedMetrics()

        # 检索层指标直接映射
        if retrieval_metrics:
            result.recall_at_5 = _safe_float(retrieval_metrics.get("recall_at_5"))
            result.mrr = _safe_float(retrieval_metrics.get("mrr"))
            result.ndcg_at_5 = _safe_float(
                retrieval_metrics.get("ndcg_at_5") or retrieval_metrics.get("ndcg")
            )

        # 生成层：RAGAS 优先，Judge 补充
        if ragas_scores:
            result.faithfulness = _safe_float(ragas_scores.get("faithfulness"))
            result.answer_relevancy = _safe_float(ragas_scores.get("answer_relevancy"))
            result.context_precision = _safe_float(ragas_scores.get("context_precision"))
            result.context_recall = _safe_float(ragas_scores.get("context_recall"))
        elif judge_scores:
            mapped = MetricsAdapter.judge_to_ragas(
                citation_accuracy=judge_scores.get("citation_accuracy", 0),
                completeness=judge_scores.get("completeness", 0),
                hallucination_inverse=judge_scores.get("hallucination_inverse", 0),
            )
            result.context_precision = mapped["context_precision"]
            result.context_recall = mapped["context_recall"]
            result.faithfulness = mapped["faithfulness"]
            # Judge 无 answer_relevancy 维度

        # Judge 原始总分
        if judge_scores:
            result.judge_total_score = _safe_float(judge_scores.get("total_score"))

        return result

    @staticmethod
    def aggregate_unified(metrics_list: list[UnifiedMetrics]) -> dict[str, float]:
        """聚合多条 UnifiedMetrics 为均值字典。

        None 值不参与均值计算。
        """
        agg: dict[str, float] = {}
        for key in UNIFIED_KEYS:
            values = [
                getattr(m, key) for m in metrics_list
                if getattr(m, key) is not None
            ]
            if values:
                agg[key] = round(sum(values) / len(values), 4)
        return agg


# ======================================================================
# 辅助函数
# ======================================================================


def _scale_to_ragas(judge_score: int | float) -> float:
    """将 Judge 0-5 分映射到 RAGAS 0-1 分。"""
    if judge_score <= 0:
        return 0.0
    return round(min(float(judge_score) / _JUDGE_MAX, _RAGAS_MAX), 4)


def _scale_to_judge(ragas_score: float) -> float:
    """将 RAGAS 0-1 分映射到 Judge 0-5 分。"""
    return round(ragas_score * _JUDGE_MAX, 2)


def _safe_float(value: Any) -> float | None:
    """安全转换为 float，None 或异常时返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
