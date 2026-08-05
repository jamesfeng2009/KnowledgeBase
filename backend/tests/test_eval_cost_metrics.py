"""P1-5 / P1-6 测试 — 延迟与 token 成本聚合 + 压缩信息损耗评估集成。

覆盖：
    - extract_cost_from_spans：wall-clock latency / total_tokens / iterations 提取
    - EvalRunResult 聚合：avg_latency / p99_latency / total_tokens
    - compare_with_baseline：延迟与成本"升高即回归"检测
    - _eval_compression_metrics：压缩 span 存在时计算实体保留率
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# P1-5: extract_cost_from_spans
# ======================================================================


class TestExtractCostFromSpans:
    def test_empty_spans_returns_defaults(self) -> None:
        from app.eval.runner import extract_cost_from_spans

        result = extract_cost_from_spans([])
        assert result["latency_ms"] is None
        assert result["total_tokens"] == 0
        assert result["iterations"] is None

    def test_wall_clock_latency_from_min_max(self) -> None:
        from app.eval.runner import extract_cost_from_spans

        spans = [
            {"start_time": 100.0, "end_time": 105.0, "parent_span_id": None},
            {"start_time": 102.0, "end_time": 110.0, "parent_span_id": "a"},
        ]
        result = extract_cost_from_spans(spans)
        # wall-clock = max(end) - min(start) = 110 - 100 = 10s = 10000ms
        assert result["latency_ms"] == 10000.0

    def test_total_tokens_from_root_metadata(self) -> None:
        from app.eval.runner import extract_cost_from_spans

        spans = [
            {
                "start_time": 1.0,
                "end_time": 2.0,
                "parent_span_id": None,
                "metadata": {"total_tokens": 1234, "iterations": 3},
            },
            {"start_time": 1.0, "end_time": 2.0, "parent_span_id": "a"},
        ]
        result = extract_cost_from_spans(spans)
        assert result["total_tokens"] == 1234
        assert result["iterations"] == 3

    def test_total_tokens_fallback_to_cost_sum(self) -> None:
        from app.eval.runner import extract_cost_from_spans

        spans = [
            {
                "start_time": 1.0,
                "end_time": 2.0,
                "parent_span_id": None,
                "metadata": {},
                "cost": {"token_count": 100},
            },
            {
                "start_time": 1.0,
                "end_time": 2.0,
                "parent_span_id": "a",
                "cost": {"token_count": 50},
            },
        ]
        result = extract_cost_from_spans(spans)
        assert result["total_tokens"] == 150


# ======================================================================
# P1-5: EvalRunResult 聚合
# ======================================================================


class TestRunAggregatesCost:
    def test_run_aggregates_latency_and_tokens(self) -> None:
        """run() 应聚合 case 级 latency/token 到 run 级 avg/p99/total。"""
        import asyncio

        from app.eval.runner import EvalCaseResult, EvalRunner

        # 构造 3 个 case 的 latency：100, 200, 300 → avg=200, p99=300
        cases = [
            MagicMock(query=f"q{i}", case_id=f"c{i}", expected_doc_ids=[],
                      expected_answer=None, kb_ids=None, context_expect=None,
                      case_type="normal", must_have_points=None,
                      forbidden_content=None)
            for i in range(3)
        ]
        # 桩 _eval_case：直接返回带 latency/token 的 EvalCaseResult
        latencies = [100.0, 200.0, 300.0]

        async def stub_eval_case(case, kb_ids, with_generation):
            idx = int(case.case_id[1:])
            return EvalCaseResult(
                query=case.query, case_id=case.case_id,
                latency_ms=latencies[idx],
                token_usage={"total_tokens": 100 * (idx + 1)},
            )

        runner = EvalRunner(engine=None)
        runner._eval_case = stub_eval_case  # type: ignore[method-assign]
        dataset = MagicMock()
        dataset.cases = cases
        dataset.fingerprint = MagicMock(return_value="v1")

        result = asyncio.run(runner.run(dataset, with_generation=False))
        assert result.avg_latency_ms == 200.0
        assert result.p99_latency_ms == 300.0
        # token: 100 + 200 + 300 = 600
        assert result.total_tokens == 600
        assert result.avg_total_tokens == 200.0


# ======================================================================
# P1-5: compare_with_baseline 延迟/成本回归（升高即回归）
# ======================================================================


class TestCompareBaselineCostRegression:
    def test_p99_latency_increase_is_regression(self) -> None:
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        cur = EvalRunResult(p99_latency_ms=2000.0, total=1)
        base = EvalRunResult(p99_latency_ms=1000.0, total=1)
        comparison = EvalRepository.compare_with_baseline(cur, base)
        m = comparison["metrics"]["p99_latency_ms"]
        assert m["regressed"] is True
        assert m["relative_drop"] == 1.0  # 上升 100%
        assert comparison["is_regression"] is True

    def test_latency_stable_no_regression(self) -> None:
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        cur = EvalRunResult(p99_latency_ms=1000.0, avg_latency_ms=500.0,
                            total_tokens=100, total=1)
        base = EvalRunResult(p99_latency_ms=1000.0, avg_latency_ms=500.0,
                             total_tokens=100, total=1)
        comparison = EvalRepository.compare_with_baseline(cur, base)
        assert comparison["metrics"]["p99_latency_ms"]["regressed"] is False
        assert comparison["metrics"]["total_tokens"]["regressed"] is False

    def test_baseline_zero_cost_no_false_regression(self) -> None:
        """基线成本为 0 时，当前有成本不判回归（避免首次引入成本误报）。"""
        from app.eval.repository import EvalRepository
        from app.eval.runner import EvalRunResult

        cur = EvalRunResult(p99_latency_ms=500.0, total=1)
        base = EvalRunResult(p99_latency_ms=0.0, total=1)
        comparison = EvalRepository.compare_with_baseline(cur, base)
        assert comparison["metrics"]["p99_latency_ms"]["regressed"] is False


# ======================================================================
# P1-6: _eval_compression_metrics
# ======================================================================


class TestEvalCompressionMetrics:
    def test_no_compact_span_skips(self) -> None:
        """无 context.compact span 时不计算压缩指标（避免跨 case 误用快照）。"""
        from app.eval.runner import EvalCaseResult, EvalRunner

        engine = MagicMock()
        engine._budget = MagicMock()
        runner = EvalRunner(engine=engine)
        result = EvalCaseResult(query="q1")

        # 普通 retrieve span，无 compact
        span = MagicMock()
        span.span_type = "context.load"
        runner._eval_compression_metrics(result, [span])
        assert result.compression_metrics is None
        engine._budget.get_last_snapshot.assert_not_called()

    def test_compact_span_triggers_entity_retention(self) -> None:
        """有 context.compact span 时读取 budget 快照计算实体保留率。"""
        from app.eval.runner import EvalCaseResult, EvalRunner

        before = [{"role": "user", "content": "报销上限 5000 元，参见《费用制度》"}]
        after = [{"role": "user", "content": "早期上下文摘要：报销上限 5000 元"}]
        budget = MagicMock()
        budget.get_last_snapshot.return_value = {
            "before": before,
            "after": after,
            "before_tokens": 100,
            "after_tokens": 30,
        }
        engine = MagicMock()
        engine._budget = budget
        runner = EvalRunner(engine=engine)
        result = EvalCaseResult(query="q1")

        compact_span = MagicMock()
        compact_span.span_type = "context.compact"
        runner._eval_compression_metrics(result, [compact_span])

        assert result.compression_metrics is not None
        assert "retention_rate" in result.compression_metrics
        budget.get_last_snapshot.assert_called_once()

    def test_no_budget_attr_skips_gracefully(self) -> None:
        """engine 无 _budget 属性时优雅跳过（不抛异常）。"""
        from app.eval.runner import EvalCaseResult, EvalRunner

        engine = MagicMock(spec=[])  # 空接口，无 _budget
        runner = EvalRunner(engine=engine)
        result = EvalCaseResult(query="q1")

        compact_span = MagicMock()
        compact_span.span_type = "context.compact"
        # 不应抛异常
        runner._eval_compression_metrics(result, [compact_span])
        assert result.compression_metrics is None
