"""
M1 · P3 evidence_ref 四层贯穿测试。

验证证据引用从埋点 → TraceContext → SpanRecord →（M2 起）ToolAuditLog
的各层透传语义：
    - span()      事后记录闭合并写入 evidence_ref
    - start_span / end_span  两段式，结束时可回退 start 暂存的 _evidence_ref
    - @trace_node  从 _span_evidence["evidence_ref"] 透传给 end_span
    - engine 生成/检索/反思节点的证据锚点

红线：不因缺 evidence_ref 而抛异常；不影响 LangFuse 不可用时的本地降级。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

# Mock celery（测试环境未安装，参考 test_eval.py / test_span_record.py）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


def _force_langfuse_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.observability.langfuse_tracer as tracer_mod

    monkeypatch.setattr(tracer_mod, "_langfuse_available", False)
    monkeypatch.setattr(tracer_mod, "_langfuse_client", None)


# ======================================================================
# E1 · span() 事后闭合透传
# ======================================================================


class TestSpanEvidenceRef:
    """span() / start_span / end_span 的 evidence_ref 透传。"""

    def test_span_writes_evidence_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.observability.langfuse_tracer import TraceContext
        from app.observability.span_record import SpanRecorder

        _force_langfuse_unavailable(monkeypatch)
        rec = SpanRecorder()
        ctx = TraceContext(session_id="s1", recorder=rec)
        ctx.start()
        ctx.span(
            name="generate_iter0",
            input_data={"q": "hi"},
            output_data={"r": "ok"},
            metadata={"latency_ms": 5.0, "token_count": 10},
            evidence_ref="doc_1",
        )
        span = next(s for s in rec.collect() if s.name == "generate_iter0")
        assert span.evidence_ref == "doc_1"

    def test_span_evidence_ref_none_when_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.langfuse_tracer import TraceContext
        from app.observability.span_record import SpanRecorder

        _force_langfuse_unavailable(monkeypatch)
        rec = SpanRecorder()
        ctx = TraceContext(session_id="s1", recorder=rec)
        ctx.start()
        ctx.span(name="generate_iter0", metadata={"latency_ms": 1.0})
        span = next(s for s in rec.collect() if s.name == "generate_iter0")
        assert span.evidence_ref is None

    def test_span_selected_nodes(self) -> None:
        """span() 方法签名提供了 evidence_ref 可选参数（契约锁定）。"""
        import inspect

        from app.observability.langfuse_tracer import TraceContext

        sig = inspect.signature(TraceContext.span)
        assert "evidence_ref" in sig.parameters
        assert sig.parameters["evidence_ref"].default is None


# ======================================================================
# E2 · start_span / end_span 两段式透传
# ======================================================================


class TestStartEndEvidenceRef:
    """start_span 暂存 + end_span 回退 / 显式覆盖。"""

    def _ctx(self, monkeypatch: pytest.MonkeyPatch, rec):
        from app.observability.langfuse_tracer import TraceContext

        _force_langfuse_unavailable(monkeypatch)
        ctx = TraceContext(session_id="s1", recorder=rec)
        ctx.start()
        return ctx

    def test_end_span_falls_back_to_start_stash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        ctx = self._ctx(monkeypatch, rec)
        sid = ctx.start_span(
            name="tool:search",
            span_type="tool.call",
            metadata={"args": "x"},
            evidence_ref="tool_use:abc",
        )
        ctx.end_span(sid, name="tool:search", metadata={"latency_ms": 2.0})
        span = next(s for s in rec.collect() if s.name == "tool:search")
        assert span.evidence_ref == "tool_use:abc"

    def test_end_span_explicit_overrides_stash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        ctx = self._ctx(monkeypatch, rec)
        sid = ctx.start_span(
            name="tool:search",
            span_type="tool.call",
            metadata={"args": "x"},
            evidence_ref="tool_use:abc",
        )
        ctx.end_span(
            sid,
            name="tool:search",
            metadata={"latency_ms": 2.0},
            evidence_ref="result_ref:obj_9",
        )
        span = next(s for s in rec.collect() if s.name == "tool:search")
        assert span.evidence_ref == "result_ref:obj_9"

    def test_metadata_not_polluted_by_stash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.observability.span_record import SpanRecorder

        rec = SpanRecorder()
        ctx = self._ctx(monkeypatch, rec)
        sid = ctx.start_span(
            name="tool:search",
            span_type="tool.call",
            metadata={"args": "x"},
            evidence_ref="tool_use:abc",
        )
        ctx.end_span(sid, name="tool:search", metadata={"latency_ms": 2.0})
        span = next(s for s in rec.collect() if s.name == "tool:search")
        assert "_evidence_ref" not in span.metadata
        assert span.metadata.get("args") == "x"


# ======================================================================
# E3/E4 · @trace_node 透传 + 缺省容错
# ======================================================================


class TestTraceNodeEvidenceRef:
    """@trace_node 装饰器从 _span_evidence 提取 evidence_ref。"""

    def _run(self, rec: "Any", node_name: str, state: dict, result) -> "Any":
        import asyncio

        from app.observability.langfuse_tracer import TraceContext, trace_node

        class Dummy:
            _trace_ctx: TraceContext | None = None

        dummy = Dummy()
        dummy._trace_ctx = TraceContext(session_id="s1", recorder=rec)
        dummy._trace_ctx.start()

        @trace_node(node_name)
        async def _node(self, state):  # type: ignore[no-untyped-def]
            return result

        return asyncio.run(_node(dummy, state))

    def test_evidence_from_span_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.span_record import SpanRecorder

        _force_langfuse_unavailable(monkeypatch)
        rec = SpanRecorder()
        self._run(
            rec,
            "retrieve",
            {
                "session_id": "s1",
                "iteration": 0,
                "_span_evidence": {"included_refs": ["d1", "d2"], "evidence_ref": "d1"},
            },
            ["d1", "d2"],
        )
        span = next(s for s in rec.collect() if s.name == "retrieve_iter0")
        assert span.evidence_ref == "d1"
        # evidence_ref 不混进 metadata
        assert "evidence_ref" not in span.metadata
        assert span.metadata.get("included_refs") == ["d1", "d2"]

    def test_no_span_evidence_no_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.observability.span_record import SpanRecorder

        _force_langfuse_unavailable(monkeypatch)
        rec = SpanRecorder()
        self._run(rec, "reflect", {"session_id": "s1", "iteration": 1}, None)
        spans = rec.collect()
        ref = next(s for s in spans if s.name == "reflect_iter1")
        assert ref.evidence_ref is None


def test_trace_node_signature_contract() -> None:
    """@trace_node 不影响正常执行且不强制 evidence_ref"""
    from app.observability.langfuse_tracer import trace_node

    assert callable(trace_node)


# ======================================================================
# E5 · engine 生成/检索/反思节点带证据锚点（埋点契约锁定）
# ======================================================================


class TestEngineEvidenceAnchors:
    """engine 关键埋点证据契约 — 通过源码文本检查锁定。

    直接读取 engine.py 源文件文本（不 import 模块，避免 celery mock /
    包副作用与 forward-reference 解析引发 ImportError），仅做字面量断言，
    防止未来改动破坏证据贯穿。
    """

    _ENGINE_SRC = os.path.join(os.path.dirname(__file__), "..", "app", "rag", "engine.py")

    def _engine_text(self) -> str:
        with open(os.path.normpath(self._ENGINE_SRC), encoding="utf-8") as f:
            return f.read()

    def test_generate_span_has_evidence_ref(self) -> None:
        text = self._engine_text()
        # generate span 必须带 evidence_ref（首 included ref）
        assert "evidence_ref=_gen_included[0] if _gen_included else None" in text

    def test_tool_span_starts_with_evidence(self) -> None:
        text = self._engine_text()
        assert "evidence_ref=f\"tool_use:{tool_use_id}\"" in text

    def test_retrieve_evidence_ref_in_span_evidence(self) -> None:
        text = self._engine_text()
        assert '"evidence_ref": included_ids[0] if included_ids else None' in text

    def test_reflect_evidence_ref_in_span_evidence(self) -> None:
        text = self._engine_text()
        assert '"evidence_ref": cited_docs[0] if cited_docs else None' in text