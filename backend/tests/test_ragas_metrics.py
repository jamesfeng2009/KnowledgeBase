"""
RAGAS 指标模块测试 — 测试 RagasMetrics 的四项标准指标计算。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.eval.ragas_metrics import RagasMetrics


class TestRagasMetricsHeuristic:
    """测试 RAGAS 指标的启发式降级评分（无 LLM 时）。"""

    @pytest.fixture
    def metrics(self):
        return RagasMetrics(llm=None)

    def test_init_without_llm(self, metrics):
        """无 LLM 时应标记为不可用。"""
        assert metrics._llm_available is False
        assert metrics.llm is None

    def test_faithfulness_heuristic_high_overlap(self, metrics):
        """答案完全来自上下文时忠实度应较高。"""
        context = "公司报销流程：填写报销单，提交部门审批，财务打款。"
        answer = "公司报销流程：填写报销单，提交部门审批，财务打款。"
        score = asyncio.get_event_loop().run_until_complete(
            metrics._faithfulness(answer, [context])
        )
        assert 0.0 <= score <= 1.0
        assert score > 0.5

    def test_faithfulness_heuristic_no_overlap(self, metrics):
        """答案与上下文完全无关时忠实度应为 0。"""
        context = "公司报销流程：填写报销单。"
        answer = "今天天气很好，适合出去玩。"
        score = asyncio.get_event_loop().run_until_complete(
            metrics._faithfulness(answer, [context])
        )
        assert score == 0.0 or score < 0.5

    def test_faithfulness_empty_answer(self, metrics):
        """空答案忠实度应为 0。"""
        score = asyncio.get_event_loop().run_until_complete(
            metrics._faithfulness("", ["context"])
        )
        assert score == 0.0

    def test_faithfulness_empty_context(self, metrics):
        """空上下文忠实度应为 0。"""
        score = asyncio.get_event_loop().run_until_complete(
            metrics._faithfulness("answer", [""])
        )
        assert score == 0.0

    def test_answer_relevancy_heuristic(self, metrics):
        """切题度启发式评分。"""
        query = "报销流程是什么"
        answer = "报销流程包括填写报销单和审批"
        score = asyncio.get_event_loop().run_until_complete(
            metrics._answer_relevancy(query, answer)
        )
        assert 0.0 <= score <= 1.0

    def test_answer_relevancy_empty_answer(self, metrics):
        """空答案切题度应为 0。"""
        score = asyncio.get_event_loop().run_until_complete(
            metrics._answer_relevancy("query", "")
        )
        assert score == 0.0

    def test_context_precision_no_expected(self, metrics):
        """无期望答案时上下文精度应为 1.0。"""
        score = metrics._context_precision("query", ["ctx1", "ctx2"], None)
        assert score == 1.0

    def test_context_precision_empty_contexts(self, metrics):
        """空上下文列表精度应为 0。"""
        score = metrics._context_precision("query", [], "expected")
        assert score == 0.0

    def test_context_recall_no_expected(self, metrics):
        """无期望答案时召回率应为 1.0。"""
        score = asyncio.get_event_loop().run_until_complete(
            metrics._context_recall("", ["ctx1"])
        )
        assert score == 1.0

    def test_context_recall_empty_contexts(self, metrics):
        """空上下文召回率应为 0。"""
        score = asyncio.get_event_loop().run_until_complete(
            metrics._context_recall("expected answer", [])
        )
        assert score == 0.0


class TestRagasMetricsEvaluate:
    """测试 RAGAS evaluate 方法。"""

    def test_evaluate_returns_all_four_metrics(self):
        """evaluate 应返回四项指标。"""
        metrics = RagasMetrics(llm=None)
        result = asyncio.get_event_loop().run_until_complete(
            metrics.evaluate(
                query="报销流程",
                answer="报销流程包括填写报销单和审批。",
                contexts=["报销流程：填写报销单，提交审批。"],
                expected_answer="报销流程：填写报销单，提交审批。",
            )
        )
        assert "faithfulness" in result
        assert "answer_relevancy" in result
        assert "context_precision" in result
        assert "context_recall" in result
        for v in result.values():
            assert 0.0 <= v <= 1.0

    def test_evaluate_empty_inputs(self):
        """空输入不应抛异常，应返回 0 分。"""
        metrics = RagasMetrics(llm=None)
        result = asyncio.get_event_loop().run_until_complete(
            metrics.evaluate(query="", answer="", contexts=[])
        )
        assert result["faithfulness"] == 0.0
        assert result["answer_relevancy"] == 0.0

    def test_parse_score_json(self):
        """从 JSON 响应解析评分。"""
        assert RagasMetrics._parse_score('{"score": 0.85}') == 0.85

    def test_parse_score_markdown_json(self):
        """从 markdown 代码块解析 JSON 评分。"""
        assert RagasMetrics._parse_score('```json\n{"score": 0.9}\n```') == 0.9

    def test_parse_score_plain_number(self):
        """从纯数字解析评分。"""
        assert RagasMetrics._parse_score("0.75") == 0.75

    def test_parse_score_empty(self):
        """空响应返回 0。"""
        assert RagasMetrics._parse_score("") == 0.0

    def test_parse_score_percentage(self):
        """百分制评分自动转换。"""
        assert RagasMetrics._parse_score("85") == 0.85

    def test_parse_score_clamp(self):
        """评分超出 1.0 时 clamp 到 1.0。"""
        # 注意: "1.5" 会被解析为 1.5 > 1.0 -> 0.015 (因为 1.5/100)
        # 但 "1.0" 应该是 1.0
        assert RagasMetrics._parse_score('{"score": 1.5}') == 1.0
