"""
上下文管理评测测试 — 评测.md §7.3 四类分数 / §9.1 ContextTraceRecord。

覆盖范围：
    - context_trace.py：from_spans 聚合（sources / included / excluded /
      trust_levels / compaction / subagent / token_cost）、去重、dict 兼容
    - context_metrics.py：recall / precision / freshness / robustness 各分支
    - dataset.py：context_expect 字段加载
    - runner.py 集成：context_expect 用例经 Span 证据计算上下文分数
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock celery（测试环境未安装，参考 test_eval.py）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# ContextTraceRecord.from_spans
# ======================================================================


class TestContextTraceRecord:
    """ContextTraceRecord 聚合测试。"""

    def test_from_spans_full_aggregation(self) -> None:
        from app.eval.context_trace import ContextTraceRecord
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        sid = rec.start_span("context.load", name="load")
        rec.end_span(sid)
        # 直接修改 metadata 注入证据（等价于 engine 埋点写入）
        spans = rec.collect()
        spans[0].metadata.update(
            {
                "source": "knowledge_base",
                "included_refs": ["src/a.ts", "src/b.ts"],
                "excluded_refs": ["logs/big.log"],
                "trust_levels": {"knowledge_base": "internal"},
                "token_cost": {"before": 8000, "after": 3000},
            }
        )

        record = ContextTraceRecord.from_spans(spans)
        assert record.context_sources == ["knowledge_base"]
        assert record.context_included_refs == ["src/a.ts", "src/b.ts"]
        assert record.context_excluded_refs == ["logs/big.log"]
        assert record.trust_levels == {"knowledge_base": "internal"}
        assert record.token_cost == {"before": 8000, "after": 3000}
        assert record.has_evidence is True

    def test_from_spans_dedup_and_merge(self) -> None:
        from app.eval.context_trace import ContextTraceRecord

        spans = [
            {"metadata": {"included_refs": ["a", "b"], "token_cost": {"before": 100}}},
            {"metadata": {"included_refs": ["b", "c"], "token_cost": {"before": 50}}},
        ]
        record = ContextTraceRecord.from_spans(spans)
        assert record.context_included_refs == ["a", "b", "c"]
        # token_cost 同键累加
        assert record.token_cost == {"before": 150}

    def test_from_spans_compaction_and_subagent(self) -> None:
        from app.eval.context_trace import ContextTraceRecord

        spans = [
            {
                "metadata": {
                    "compaction_event": {
                        "reason": "token_budget",
                        "preserved_refs": ["constraints/no_prod_write"],
                        "dropped": ["old_logs"],
                    }
                }
            },
            {
                "metadata": {
                    "subagent_summary": {
                        "task": "search",
                        "evidence_refs": ["doc_1"],
                    }
                }
            },
        ]
        record = ContextTraceRecord.from_spans(spans)
        assert len(record.compaction_events) == 1
        assert record.compaction_events[0]["reason"] == "token_budget"
        assert len(record.subagent_summaries) == 1
        assert record.subagent_summaries[0]["task"] == "search"

    def test_from_spans_empty(self) -> None:
        from app.eval.context_trace import ContextTraceRecord

        record = ContextTraceRecord.from_spans([])
        assert record.has_evidence is False
        assert record.to_dict()["context_sources"] == []

    def test_from_spans_ignores_non_dict_metadata(self) -> None:
        from app.eval.context_trace import ContextTraceRecord

        record = ContextTraceRecord.from_spans([{"metadata": "broken"}])
        assert record.has_evidence is False


# ======================================================================
# compute_context_metrics
# ======================================================================


class TestComputeContextMetrics:
    """四类上下文分数计算测试。"""

    def _record(self, included: list[str], **kwargs) -> object:
        from app.eval.context_trace import ContextTraceRecord

        return ContextTraceRecord(context_included_refs=included, **kwargs)

    def test_none_expect_returns_none(self) -> None:
        from app.eval.context_metrics import compute_context_metrics

        assert compute_context_metrics(self._record([]), None) is None
        assert compute_context_metrics(self._record([]), {}) is None

    def test_recall_full_and_partial(self) -> None:
        from app.eval.context_metrics import compute_context_metrics

        # 全部进入 → recall 1.0
        r = compute_context_metrics(
            self._record(["src/payments/token.ts"]),
            {"required_files": ["src/payments/token.ts"]},
        )
        assert r is not None
        assert r["recall"] == 1.0
        assert r["missing_required"] == []
        assert r["passed"] is True

        # 漏掉一半 → recall 0.5
        r2 = compute_context_metrics(
            self._record(["a"]),
            {"required_files": ["a", "b"]},
        )
        assert r2 is not None
        assert r2["recall"] == 0.5
        assert r2["missing_required"] == ["b"]
        assert r2["passed"] is False

    def test_precision_distractor_and_forbidden(self) -> None:
        from app.eval.context_metrics import compute_context_metrics

        r = compute_context_metrics(
            self._record(["src/order/fsm.ts", "src/order/fsm_deprecated.ts"]),
            {
                "required_files": ["src/order/fsm.ts"],
                "distractor_files": ["src/order/fsm_deprecated.ts"],
            },
        )
        assert r is not None
        assert r["precision"] == 0.0
        assert r["included_distractors"] == ["src/order/fsm_deprecated.ts"]
        assert r["passed"] is False

        # 干扰未进入 → precision 满分
        r2 = compute_context_metrics(
            self._record(["src/order/fsm.ts"]),
            {
                "required_files": ["src/order/fsm.ts"],
                "distractor_files": ["src/order/fsm_deprecated.ts"],
            },
        )
        assert r2 is not None
        assert r2["precision"] == 1.0
        assert r2["passed"] is True

    def test_freshness_stale_ref(self) -> None:
        from app.eval.context_metrics import compute_context_metrics

        r = compute_context_metrics(
            self._record(["constraints/allow_direct_write_v0"]),
            {"stale_refs": ["constraints/allow_direct_write_v0"]},
        )
        assert r is not None
        assert r["freshness"] == 0.0
        assert r["included_stale"] == ["constraints/allow_direct_write_v0"]
        assert r["passed"] is False

    def test_robustness_after_compact(self) -> None:
        from app.eval.context_metrics import compute_context_metrics
        from app.eval.context_trace import ContextTraceRecord

        # 压缩事件保留了约束 → robustness 满分
        record = ContextTraceRecord(
            compaction_events=[
                {"preserved_refs": ["constraints/no_direct_prod_write"]}
            ]
        )
        r = compute_context_metrics(
            record,
            {"required_after_compact": ["constraints/no_direct_prod_write"]},
        )
        assert r is not None
        assert r["robustness"] == 1.0
        assert r["lost_after_compact"] == []

        # 压缩后丢失约束 → robustness 0
        record2 = ContextTraceRecord(compaction_events=[{"preserved_refs": []}])
        r2 = compute_context_metrics(
            record2,
            {"required_after_compact": ["constraints/backup_first"]},
        )
        assert r2 is not None
        assert r2["robustness"] == 0.0
        assert r2["lost_after_compact"] == ["constraints/backup_first"]
        assert r2["passed"] is False

    def test_robustness_na_without_compaction(self) -> None:
        from app.eval.context_metrics import compute_context_metrics

        # 有 required_after_compact 期望但无压缩事件 → 不适用，满分
        r = compute_context_metrics(
            self._record([]),
            {"required_after_compact": ["c1"]},
        )
        assert r is not None
        assert r["robustness"] == 1.0
        assert r["passed"] is True

    def test_expect_without_known_fields_returns_none(self) -> None:
        from app.eval.context_metrics import compute_context_metrics

        assert compute_context_metrics(
            self._record([]), {"type": "untrusted_web"}
        ) is None


# ======================================================================
# EvalCase context_expect 字段
# ======================================================================


class TestEvalCaseContextExpect:
    """EvalCase context_expect 加载。"""

    def test_from_dict_with_context_expect(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase.from_dict(
            {
                "query": "q",
                "context_expect": {
                    "type": "required_file",
                    "required_files": ["src/a.ts"],
                },
            }
        )
        assert case.context_expect["type"] == "required_file"
        assert case.context_expect["required_files"] == ["src/a.ts"]

    def test_from_dict_without_context_expect(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase.from_dict({"query": "q"})
        assert case.context_expect == {}

    def test_to_dict_roundtrip(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase(
            query="q", context_expect={"required_files": ["a"]}
        )
        restored = EvalCase.from_dict(case.to_dict())
        assert restored.context_expect == {"required_files": ["a"]}


# ======================================================================
# Runner 集成
# ======================================================================


class TestRunnerContextIntegration:
    """EvalRunner 上下文评分集成。"""

    def _make_engine_with_context_span(
        self, included: list[str]
    ) -> MagicMock:
        """构造 mock engine：检索时发出携带上下文证据的 Span。"""
        from app.observability.langfuse_tracer import TraceContext

        engine = MagicMock()

        async def fake_retrieve(state: dict, kb_ids: object = None) -> None:
            ctx = TraceContext(session_id=state.get("session_id", ""))
            ctx.span(
                name="context.load",
                metadata={
                    "source": "knowledge_base",
                    "included_refs": included,
                    "latency_ms": 1.0,
                },
            )
            state["retrieved_docs"] = []

        engine._retrieve = fake_retrieve
        return engine

    @pytest.mark.asyncio
    async def test_context_metrics_computed_passed(self) -> None:
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = self._make_engine_with_context_span(["src/payments/token.ts"])
        runner = EvalRunner(engine=engine)
        case = EvalCase(
            query="支付 token 校验",
            context_expect={
                "type": "required_file",
                "required_files": ["src/payments/token.ts"],
                "forbidden_files": ["old_payments/token_legacy.ts"],
            },
        )
        result = await runner.run([case], with_generation=False)
        cr = result.case_results[0]
        assert cr.context_metrics is not None
        assert cr.context_metrics["recall"] == 1.0
        assert cr.context_metrics["precision"] == 1.0
        assert cr.context_metrics["passed"] is True
        assert cr.passed is True

    @pytest.mark.asyncio
    async def test_context_metrics_failure_fails_case(self) -> None:
        """读了干扰文件 → precision 不合格 → 用例不通过。"""
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = self._make_engine_with_context_span(
            ["src/order/fsm.ts", "src/order/fsm_deprecated.ts"]
        )
        runner = EvalRunner(engine=engine)
        case = EvalCase(
            query="订单状态机",
            context_expect={
                "required_files": ["src/order/fsm.ts"],
                "distractor_files": ["src/order/fsm_deprecated.ts"],
            },
        )
        result = await runner.run([case], with_generation=False)
        cr = result.case_results[0]
        assert cr.context_metrics is not None
        assert cr.context_metrics["precision"] == 0.0
        assert cr.context_metrics["passed"] is False
        assert cr.passed is False
        assert result.passed == 0

    @pytest.mark.asyncio
    async def test_no_context_expect_no_metrics(self) -> None:
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = self._make_engine_with_context_span(["a"])
        runner = EvalRunner(engine=engine)
        result = await runner.run(
            [EvalCase(query="q", expected_doc_ids=[])],
            with_generation=False,
        )
        assert result.case_results[0].context_metrics is None

    @pytest.mark.asyncio
    async def test_context_metrics_serialize_roundtrip(self) -> None:
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = self._make_engine_with_context_span(["src/a.ts"])
        runner = EvalRunner(engine=engine)
        result = await runner.run(
            [EvalCase(query="q", context_expect={"required_files": ["src/a.ts"]})],
            with_generation=False,
        )
        d = result.to_dict()
        assert d["case_results"][0]["context_metrics"]["recall"] == 1.0
        assert d["case_results"][0]["passed"] is True
