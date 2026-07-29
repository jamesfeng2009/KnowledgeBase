"""
EvalRunner RAGAS 集成测试 — 测试 RAGAS 指标在评测流程中的集成。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.eval.runner import (
    EvalCaseResult,
    EvalRunner,
    EvalRunResult,
)


@dataclass
class MockEvalCase:
    """模拟评测用例。"""
    query: str
    expected_doc_ids: list[str]
    expected_answer: str | None = None
    kb_ids: list[str] | None = None


class TestEvalRunnerRagasIntegration:
    """测试 EvalRunner 与 RAGAS 的集成。"""

    @pytest.fixture
    def mock_ragas(self):
        """模拟 RagasMetrics。"""
        ragas = MagicMock()
        ragas.evaluate = AsyncMock(return_value={
            "faithfulness": 0.85,
            "answer_relevancy": 0.90,
            "context_precision": 0.80,
            "context_recall": 0.75,
        })
        return ragas

    @pytest.fixture
    def mock_engine(self):
        """模拟 RAG 引擎。"""
        engine = MagicMock()

        # 模拟 _retrieve
        async def mock_retrieve(state, kb_ids):
            state["retrieved_docs"] = [
                {"doc_id": "doc_001", "content": "这是关于报销流程的文档内容"},
                {"doc_id": "doc_002", "content": "审批流程需要部门经理签字"},
            ]

        engine._retrieve = mock_retrieve

        # 模拟 answer（异步生成器）
        async def mock_answer(query, user_id, session_id, **kwargs):
            yield "报销流程包括填写报销单和审批。"

        engine.answer = mock_answer
        return engine

    @pytest.fixture
    def mock_judge(self):
        """模拟 LLMJudgeService。"""
        judge = MagicMock()
        eval_result = MagicMock()
        eval_result.citation_accuracy = 4
        eval_result.completeness = 5
        eval_result.hallucination_inverse = 4
        eval_result.total_score = 4.33
        eval_result.passed = True
        judge.evaluate_single = AsyncMock(return_value=eval_result)
        return judge

    def test_eval_case_result_has_ragas_scores(self):
        """EvalCaseResult 包含 ragas_scores 字段。"""
        result = EvalCaseResult(
            query="test",
            ragas_scores={"faithfulness": 0.8},
        )
        assert result.ragas_scores == {"faithfulness": 0.8}

    def test_eval_case_result_to_dict_includes_ragas(self):
        """to_dict 包含 ragas_scores。"""
        result = EvalCaseResult(
            query="test",
            ragas_scores={"faithfulness": 0.85},
        )
        d = result.to_dict()
        assert "ragas_scores" in d
        assert d["ragas_scores"]["faithfulness"] == 0.85

    def test_eval_run_result_has_avg_ragas(self):
        """EvalRunResult 包含 avg_ragas 字段。"""
        result = EvalRunResult(
            avg_ragas={"faithfulness": 0.85, "answer_relevancy": 0.90},
        )
        assert result.avg_ragas["faithfulness"] == 0.85

    def test_eval_run_result_to_dict_includes_ragas(self):
        """to_dict 包含 avg_ragas。"""
        result = EvalRunResult(
            avg_ragas={"faithfulness": 0.85},
        )
        d = result.to_dict()
        assert "avg_ragas" in d
        assert d["avg_ragas"]["faithfulness"] == 0.85

    def test_runner_with_ragas_metrics(self, mock_engine, mock_judge, mock_ragas):
        """Runner 传入 ragas_metrics 后应计算 RAGAS 指标。"""
        runner = EvalRunner(
            engine=mock_engine,
            judge_service=mock_judge,
            ragas_metrics=mock_ragas,
        )
        assert runner.ragas_metrics is not None

        dataset = [MockEvalCase(
            query="报销流程是什么",
            expected_doc_ids=["doc_001"],
            expected_answer="报销流程包括填写报销单和审批。",
        )]

        result = asyncio.run(
            runner.run(dataset, with_generation=True)
        )

        # 应有 RAGAS 指标
        assert len(result.avg_ragas) == 4
        assert result.avg_ragas["faithfulness"] == 0.85
        assert result.avg_ragas["answer_relevancy"] == 0.90
        assert result.avg_ragas["context_precision"] == 0.80
        assert result.avg_ragas["context_recall"] == 0.75

        # 用例结果也应有 ragas_scores
        for case_result in result.case_results:
            assert case_result.ragas_scores is not None

    def test_runner_without_ragas_metrics(self, mock_engine, mock_judge):
        """不传 ragas_metrics 时应优雅降级。"""
        runner = EvalRunner(
            engine=mock_engine,
            judge_service=mock_judge,
            ragas_metrics=None,
        )
        assert runner.ragas_metrics is None

        dataset = [MockEvalCase(
            query="test",
            expected_doc_ids=["doc_001"],
        )]

        result = asyncio.run(
            runner.run(dataset, with_generation=True)
        )

        # avg_ragas 应为空字典
        assert result.avg_ragas == {}
        # 用例结果 ragas_scores 应为 None
        for case_result in result.case_results:
            assert case_result.ragas_scores is None

    def test_runner_ragas_error_graceful_degradation(self, mock_engine, mock_judge):
        """RAGAS 计算异常不应影响整体评测。"""
        ragas = MagicMock()
        ragas.evaluate = AsyncMock(side_effect=RuntimeError("LLM error"))

        runner = EvalRunner(
            engine=mock_engine,
            judge_service=mock_judge,
            ragas_metrics=ragas,
        )

        dataset = [MockEvalCase(
            query="test",
            expected_doc_ids=["doc_001"],
        )]

        result = asyncio.run(
            runner.run(dataset, with_generation=True)
        )

        # 评测不应崩溃
        assert result.total == 1
        assert result.passed >= 0
        # avg_ragas 应为空（因为全部失败）
        assert result.avg_ragas == {}

    def test_runner_retrieval_only_skips_ragas(self, mock_engine, mock_ragas):
        """只测检索（with_generation=False）时不调用 RAGAS。"""
        runner = EvalRunner(
            engine=mock_engine,
            ragas_metrics=mock_ragas,
        )

        dataset = [MockEvalCase(
            query="test",
            expected_doc_ids=["doc_001"],
        )]

        result = asyncio.run(
            runner.run(dataset, with_generation=False)
        )

        # RAGAS 未被调用
        mock_ragas.evaluate.assert_not_called()
        assert result.avg_ragas == {}
