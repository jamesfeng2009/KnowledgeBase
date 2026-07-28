"""
统一评测指标适配器测试 — 测试 MetricsAdapter 的维度转换和指标合并。
"""

from __future__ import annotations

import pytest

from app.eval.unified_metrics import (
    MetricsAdapter,
    UnifiedMetrics,
    UNIFIED_KEYS,
)


class TestJudgeToRagas:
    """测试 LLMJudge 0-5 分到 RAGAS 0-1 分的转换。"""

    def test_max_scores(self):
        """满分转换。"""
        result = MetricsAdapter.judge_to_ragas(
            citation_accuracy=5, completeness=5, hallucination_inverse=5
        )
        assert result["context_precision"] == 1.0
        assert result["context_recall"] == 1.0
        assert result["faithfulness"] == 1.0

    def test_zero_scores(self):
        """零分转换。"""
        result = MetricsAdapter.judge_to_ragas(
            citation_accuracy=0, completeness=0, hallucination_inverse=0
        )
        assert result["context_precision"] == 0.0
        assert result["context_recall"] == 0.0
        assert result["faithfulness"] == 0.0

    def test_mid_scores(self):
        """中等分数转换。"""
        result = MetricsAdapter.judge_to_ragas(
            citation_accuracy=4, completeness=3, hallucination_inverse=4
        )
        assert result["context_precision"] == 0.8
        assert result["context_recall"] == 0.6
        assert result["faithfulness"] == 0.8

    def test_negative_scores_clamped(self):
        """负分应 clamp 到 0。"""
        result = MetricsAdapter.judge_to_ragas(
            citation_accuracy=-1, completeness=-5, hallucination_inverse=-10
        )
        assert result["context_precision"] == 0.0
        assert result["context_recall"] == 0.0
        assert result["faithfulness"] == 0.0


class TestRagasToJudge:
    """测试 RAGAS 0-1 分到 LLMJudge 0-5 分的转换。"""

    def test_max_scores(self):
        """满分转换。"""
        result = MetricsAdapter.ragas_to_judge(
            faithfulness=1.0,
            answer_relevancy=1.0,
            context_precision=1.0,
            context_recall=1.0,
        )
        assert result["citation_accuracy"] == 5.0
        assert result["hallucination_inverse"] == 5.0
        assert result["completeness"] == 5.0
        assert result["total_score"] == 5.0

    def test_zero_scores(self):
        """零分转换。"""
        result = MetricsAdapter.ragas_to_judge(
            faithfulness=0.0,
            answer_relevancy=0.0,
            context_precision=0.0,
            context_recall=0.0,
        )
        assert result["citation_accuracy"] == 0.0
        assert result["total_score"] == 0.0

    def test_completeness_weighted(self):
        """completeness = context_recall * 0.7 + answer_relevancy * 0.3。"""
        result = MetricsAdapter.ragas_to_judge(
            faithfulness=0.5,
            answer_relevancy=1.0,
            context_precision=0.5,
            context_recall=0.0,
        )
        # completeness = (0.0 * 0.7 + 1.0 * 0.3) * 5 = 1.5
        assert result["completeness"] == 1.5


class TestUnifyCaseResult:
    """测试多来源指标合并。"""

    def test_retrieval_only(self):
        """仅检索层指标。"""
        result = MetricsAdapter.unify_case_result(
            retrieval_metrics={"recall_at_5": 0.8, "mrr": 0.5, "ndcg_at_5": 0.6}
        )
        assert result.recall_at_5 == 0.8
        assert result.mrr == 0.5
        assert result.ndcg_at_5 == 0.6
        assert result.faithfulness is None
        assert result.judge_total_score is None

    def test_ragas_priority_over_judge(self):
        """RAGAS 优先于 Judge。"""
        result = MetricsAdapter.unify_case_result(
            retrieval_metrics={"recall_at_5": 1.0},
            judge_scores={"citation_accuracy": 3, "completeness": 4, "hallucination_inverse": 5, "total_score": 4.0},
            ragas_scores={"faithfulness": 0.9, "answer_relevancy": 0.8, "context_precision": 0.7, "context_recall": 0.6},
        )
        # RAGAS 值优先
        assert result.faithfulness == 0.9
        assert result.context_precision == 0.7
        # Judge 原始总分仍保留
        assert result.judge_total_score == 4.0

    def test_judge_fallback_when_no_ragas(self):
        """无 RAGAS 时使用 Judge 转换。"""
        result = MetricsAdapter.unify_case_result(
            judge_scores={
                "citation_accuracy": 4,
                "completeness": 5,
                "hallucination_inverse": 3,
                "total_score": 4.0,
            }
        )
        assert result.context_precision == 0.8  # 4/5
        assert result.context_recall == 1.0  # 5/5
        assert result.faithfulness == 0.6  # 3/5
        assert result.answer_relevancy is None  # Judge 无此维度
        assert result.judge_total_score == 4.0

    def test_empty_inputs(self):
        """空输入返回全 None。"""
        result = MetricsAdapter.unify_case_result()
        assert result.recall_at_5 is None
        assert result.faithfulness is None
        assert result.generation_score is None

    def test_generation_score_property(self):
        """generation_score 计算 RAGAS 均值。"""
        result = UnifiedMetrics(
            faithfulness=0.8,
            answer_relevancy=0.9,
            context_precision=0.7,
            context_recall=0.6,
        )
        expected = (0.8 + 0.9 + 0.7 + 0.6) / 4
        assert abs(result.generation_score - expected) < 0.001

    def test_generation_score_none(self):
        """无生成层指标时 generation_score 为 None。"""
        result = UnifiedMetrics(recall_at_5=0.8)
        assert result.generation_score is None


class TestAggregateUnified:
    """测试聚合统计。"""

    def test_aggregate_basic(self):
        """基本聚合。"""
        metrics_list = [
            UnifiedMetrics(recall_at_5=0.8, mrr=0.5),
            UnifiedMetrics(recall_at_5=0.6, mrr=0.4),
            UnifiedMetrics(recall_at_5=1.0, mrr=0.8),
        ]
        agg = MetricsAdapter.aggregate_unified(metrics_list)
        assert agg["recall_at_5"] == 0.8  # (0.8+0.6+1.0)/3
        assert agg["mrr"] == round((0.5 + 0.4 + 0.8) / 3, 4)

    def test_aggregate_with_none(self):
        """None 值不参与均值。"""
        metrics_list = [
            UnifiedMetrics(recall_at_5=0.8, faithfulness=0.9),
            UnifiedMetrics(recall_at_5=0.6),  # faithfulness = None
        ]
        agg = MetricsAdapter.aggregate_unified(metrics_list)
        assert agg["recall_at_5"] == 0.7  # (0.8+0.6)/2
        assert agg["faithfulness"] == 0.9  # 只有第一条有值

    def test_aggregate_empty_list(self):
        """空列表返回空字典。"""
        agg = MetricsAdapter.aggregate_unified([])
        assert agg == {}

    def test_unified_keys_completeness(self):
        """UNIFIED_KEYS 包含所有统一指标。"""
        assert "recall_at_5" in UNIFIED_KEYS
        assert "mrr" in UNIFIED_KEYS
        assert "ndcg_at_5" in UNIFIED_KEYS
        assert "faithfulness" in UNIFIED_KEYS
        assert "answer_relevancy" in UNIFIED_KEYS
        assert "context_precision" in UNIFIED_KEYS
        assert "context_recall" in UNIFIED_KEYS
        assert "judge_total_score" in UNIFIED_KEYS
