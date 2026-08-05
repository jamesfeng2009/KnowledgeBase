"""
标准 Span 落地测试 — 评测.md §4.4 双写架构。

覆盖范围：
    - span_record.py：SpanRecord 序列化、SpanRecorder 生命周期、父子关系、
      record_closed、collect 超时标记、contextvar 注入
    - langfuse_tracer.py：TraceContext 双写（LangFuse 不可用时本地记录仍生效）
    - tool_audit.py：persist_tool_spans 关键类型过滤与字段映射
    - runner.py 集成：EvalCaseResult.spans 收集与序列化往返
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
# span_record.py — SpanRecord
# ======================================================================


class TestSpanRecord:
    """SpanRecord 数据类测试。"""

    def test_latency_ms_none_when_open(self) -> None:
        from app.observability.span_record import SpanRecord

        rec = SpanRecord(
            span_id="s1",
            parent_span_id=None,
            span_type="tool.call",
            name="knowledge_search",
            start_time=100.0,
        )
        assert rec.latency_ms is None

    def test_latency_ms_computed(self) -> None:
        from app.observability.span_record import SpanRecord

        rec = SpanRecord(
            span_id="s1",
            parent_span_id=None,
            span_type="tool.call",
            name="knowledge_search",
            start_time=100.0,
            end_time=100.25,
        )
        assert rec.latency_ms == 250.0

    def test_to_dict_roundtrip(self) -> None:
        from app.observability.span_record import SpanRecord

        rec = SpanRecord(
            span_id="s1",
            parent_span_id="s0",
            span_type="tool.call",
            name="knowledge_search",
            start_time=100.0,
            end_time=100.1,
            status="ok",
            input_ref="in",
            output_ref="out",
            error=None,
            cost={"latency_ms": 100.0},
            evidence_ref="doc_1",
            metadata={"k": "v"},
        )
        d = rec.to_dict()
        assert d["span_id"] == "s1"
        assert d["parent_span_id"] == "s0"
        assert d["span_type"] == "tool.call"
        assert d["latency_ms"] == 100.0
        assert d["status"] == "ok"
        assert d["input_ref"] == "in"
        assert d["output_ref"] == "out"
        assert d["cost"] == {"latency_ms": 100.0}
        assert d["evidence_ref"] == "doc_1"
        assert d["metadata"] == {"k": "v"}


# ======================================================================
# span_record.py — SpanRecorder 生命周期
# ======================================================================


class TestSpanRecorder:
    """SpanRecorder 生命周期与父子关系测试。"""

    def test_start_end_span(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        sid = rec.start_span("tool.call", name="t1", input_ref="q")
        rec.end_span(sid, status="ok", output_ref="r")

        spans = rec.collect()
        assert len(spans) == 1
        assert spans[0].span_id == sid
        assert spans[0].span_type == "tool.call"
        assert spans[0].name == "t1"
        assert spans[0].status == "ok"
        assert spans[0].output_ref == "r"
        assert spans[0].end_time is not None
        assert spans[0].latency_ms is not None

    def test_parent_child_via_stack(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        parent = rec.start_span("plan.create", name="think")
        child = rec.start_span("tool.call", name="search")
        rec.end_span(child)
        rec.end_span(parent)

        spans = rec.collect()
        assert len(spans) == 2
        child_rec = next(s for s in spans if s.name == "search")
        parent_rec = next(s for s in spans if s.name == "think")
        assert child_rec.parent_span_id == parent_rec.span_id
        assert parent_rec.parent_span_id is None

    def test_end_unknown_span_logs_no_crash(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        rec.end_span("nonexistent")  # 不应抛异常
        assert len(rec.collect()) == 0

    def test_context_manager_span_ok(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        with rec.span("tool.call", name="t1"):
            pass

        spans = rec.collect()
        assert len(spans) == 1
        assert spans[0].status == "ok"
        assert spans[0].end_time is not None

    def test_context_manager_span_error(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        with pytest.raises(ValueError), rec.span("tool.call", name="t1"):
            raise ValueError("boom")

        spans = rec.collect()
        assert len(spans) == 1
        assert spans[0].status == "error"
        assert spans[0].error == "boom"

    def test_record_closed(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        rec.record_closed(
            name="think_iter1",
            input_ref="in",
            output_ref="out",
            error=None,
            cost={"latency_ms": 12.5},
            metadata={"iteration": 1},
        )
        spans = rec.collect()
        assert len(spans) == 1
        s = spans[0]
        assert s.name == "think_iter1"
        assert s.span_type == "think_iter1"  # span_type 缺省取 name
        assert s.status == "ok"
        assert s.cost == {"latency_ms": 12.5}
        assert s.metadata == {"iteration": 1}

    def test_record_closed_with_error(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        rec.record_closed(name="retrieve", error="timeout")
        spans = rec.collect()
        assert spans[0].status == "error"
        assert spans[0].error == "timeout"

    def test_collect_marks_unclosed_as_timeout(self) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        rec.start_span("tool.call", name="never_ended")
        spans = rec.collect()
        assert len(spans) == 1
        assert spans[0].status == "timeout"
        assert spans[0].end_time is not None


# ======================================================================
# span_record.py — contextvar 注入
# ======================================================================


class TestSpanRecorderContextVar:
    """span_recorder / get_current_recorder contextvar 集成测试。"""

    def test_no_recorder_by_default(self) -> None:
        from app.observability.span_record import get_current_recorder

        assert get_current_recorder() is None

    def test_recorder_active_in_context(self) -> None:
        from app.observability.span_record import (
            get_current_recorder,
            span_recorder,
        )

        with span_recorder() as rec:
            assert get_current_recorder() is rec
        assert get_current_recorder() is None

    def test_nested_contexts_isolated(self) -> None:
        from app.observability.span_record import (
            SpanRecorder,
            get_current_recorder,
            span_recorder,
        )

        outer = SpanRecorder()
        with span_recorder(outer):
            assert get_current_recorder() is outer
            with span_recorder() as inner:
                assert get_current_recorder() is inner
                assert inner is not outer
            assert get_current_recorder() is outer
        assert get_current_recorder() is None


# ======================================================================
# langfuse_tracer.py — TraceContext 双写
# ======================================================================


class TestTraceContextDualWrite:
    """TraceContext 双写到 SpanRecorder 测试（LangFuse 不可用也应生效）。"""

    def _force_langfuse_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.observability.langfuse_tracer as tracer_mod

        monkeypatch.setattr(tracer_mod, "_langfuse_available", False)
        monkeypatch.setattr(tracer_mod, "_langfuse_client", None)

    def test_explicit_recorder_receives_spans(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.langfuse_tracer import TraceContext
        from app.observability.span_record import SpanRecorder

        self._force_langfuse_unavailable(monkeypatch)
        rec = SpanRecorder()
        ctx = TraceContext(session_id="s1", recorder=rec)
        ctx.start()  # LangFuse 不可用，静默跳过
        ctx.span(
            name="think_iter1",
            input_data={"q": "hi"},
            output_data={"r": "ok"},
            metadata={"latency_ms": 5.0, "token_count": 10},
        )

        spans = rec.collect()
        assert len(spans) == 1
        s = spans[0]
        assert s.name == "think_iter1"
        assert s.status == "ok"
        assert s.cost.get("latency_ms") == 5.0
        assert s.cost.get("token_count") == 10

    def test_recorder_picked_from_contextvar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.langfuse_tracer import TraceContext
        from app.observability.span_record import span_recorder

        self._force_langfuse_unavailable(monkeypatch)
        with span_recorder() as rec:
            ctx = TraceContext(session_id="s1")
            ctx.span(name="retrieve", metadata={"latency_ms": 3.0})
        spans = rec.collect()
        assert len(spans) == 1
        assert spans[0].name == "retrieve"

    def test_no_recorder_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.observability.langfuse_tracer import TraceContext

        self._force_langfuse_unavailable(monkeypatch)
        ctx = TraceContext(session_id="s1", recorder=None)
        ctx.start()
        ctx.span(name="generate")  # 双写均不可用，静默降级不抛异常
        ctx.finalize(output="done")

    def test_error_metadata_marks_span_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.langfuse_tracer import TraceContext
        from app.observability.span_record import SpanRecorder

        self._force_langfuse_unavailable(monkeypatch)
        rec = SpanRecorder()
        ctx = TraceContext(recorder=rec)
        ctx.span(name="retrieve", metadata={"error": "db down", "latency_ms": 1.0})
        spans = rec.collect()
        assert spans[0].status == "error"
        assert spans[0].error == "db down"


# ======================================================================
# tool_audit.py — persist_tool_spans
# ======================================================================


class TestPersistToolSpans:
    """persist_tool_spans 过滤与字段映射测试。"""

    def _make_span(
        self,
        span_type: str,
        name: str = "t",
        status: str = "ok",
        latency_ms: float | None = 12.0,
    ) -> MagicMock:
        span = MagicMock()
        span.span_type = span_type
        span.name = name
        span.span_id = "span123"
        span.status = status
        span.metadata = {"arg": 1}
        span.output_ref = "result"
        span.error = "err" if status != "ok" else None
        span.latency_ms = latency_ms
        span.cost = {}
        return span

    @pytest.mark.asyncio
    async def test_filters_only_audited_types(self) -> None:
        from app.models.tool_audit import persist_tool_spans

        session = MagicMock()
        spans = [
            self._make_span("tool.call"),
            self._make_span("plan.create"),  # 非审计类型，应跳过
            self._make_span("permission.decision"),
            self._make_span("failure.recover"),
            self._make_span("memory.read"),  # 非审计类型，应跳过
        ]
        written = await persist_tool_spans(
            spans, session, run_id="r1", session_id="s1"
        )
        assert written == 3
        assert session.add.call_count == 3

    @pytest.mark.asyncio
    async def test_field_mapping(self) -> None:
        from app.models.tool_audit import ToolAuditLog, persist_tool_spans

        added: list[ToolAuditLog] = []
        session = MagicMock()
        session.add.side_effect = added.append

        span = self._make_span("tool.call", name="knowledge_search", status="error")
        written = await persist_tool_spans(
            [span], session, run_id="r1", session_id="s1"
        )
        assert written == 1
        record = added[0]
        assert record.run_id == "r1"
        assert record.session_id == "s1"
        assert record.tool_name == "knowledge_search"
        assert record.arguments == {"arg": 1}
        assert record.result_summary == "result"
        assert record.error == "err"
        assert record.duration_ms == 12
        assert record.status == "error"

    @pytest.mark.asyncio
    async def test_ok_status_mapped_to_success(self) -> None:
        from app.models.tool_audit import persist_tool_spans

        added: list = []
        session = MagicMock()
        session.add.side_effect = added.append
        await persist_tool_spans(
            [self._make_span("tool.call", status="ok")],
            session,
            run_id="r",
            session_id="s",
        )
        assert added[0].status == "success"


# ======================================================================
# runner.py 集成 — spans 收集
# ======================================================================


class TestEvalRunnerSpanIntegration:
    """EvalRunner span 收集集成测试。"""

    @pytest.mark.asyncio
    async def test_case_result_contains_spans(self) -> None:
        """engine 节点埋点应被收集进 EvalCaseResult.spans。"""
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner
        from app.observability.langfuse_tracer import TraceContext

        engine = MagicMock()

        async def fake_retrieve(state: dict, kb_ids: object = None) -> None:
            # 模拟 engine 内的 @trace_node 埋点路径：经 TraceContext 双写
            ctx = TraceContext(session_id=state.get("session_id", ""))
            ctx.span(name="retrieve_iter1", metadata={"latency_ms": 2.0})
            state["retrieved_docs"] = [{"doc_id": "doc_1", "content": "x"}]

        engine._retrieve = fake_retrieve
        runner = EvalRunner(engine=engine)
        case = EvalCase(query="q", expected_doc_ids=["doc_1"])
        result = await runner.run([case], with_generation=False)

        assert result.total == 1
        case_result = result.case_results[0]
        assert case_result.error is None
        assert case_result.recall_at_5 == 1.0
        assert len(case_result.spans) == 1
        assert case_result.spans[0]["name"] == "retrieve_iter1"
        assert case_result.spans[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_spans_serialize_roundtrip(self) -> None:
        """spans 应随 to_dict 序列化，支持持久化。"""
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner
        from app.observability.langfuse_tracer import TraceContext

        engine = MagicMock()

        async def fake_retrieve(state: dict, kb_ids: object = None) -> None:
            ctx = TraceContext(session_id="s")
            ctx.span(name="retrieve_iter1", metadata={"latency_ms": 1.0})
            state["retrieved_docs"] = []

        engine._retrieve = fake_retrieve
        runner = EvalRunner(engine=engine)
        result = await runner.run(
            [EvalCase(query="q", expected_doc_ids=[])],
            with_generation=False,
        )
        d = result.to_dict()
        assert "spans" in d["case_results"][0]
        assert d["case_results"][0]["spans"][0]["name"] == "retrieve_iter1"

    @pytest.mark.asyncio
    async def test_engine_none_still_returns_empty_spans(self) -> None:
        """engine 不可用时 spans 为空列表，不抛异常。"""
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        runner = EvalRunner(engine=None)
        result = await runner.run(
            [EvalCase(query="q", expected_doc_ids=["x"])],
            with_generation=False,
        )
        assert result.case_results[0].spans == []
        assert result.case_results[0].error == "engine_unavailable"
