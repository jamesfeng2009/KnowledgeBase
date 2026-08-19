"""
M2 · P2 state 差分 + 工具审计补全测试。

覆盖：
    - engine._state_fingerprint：标量指纹的稳定性、缺键容错（S1-S3）
    - engine generate span 的 state_before/state_after 差分落 metadata（S1-S2）
    - tool_audit.persist_tool_spans：result_ref / result_full / evidence_ref
      三级降级写入（S4-S8）
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock celery（参考 test_eval.py / test_span_record.py）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# S1-S3 · _state_fingerprint
# ======================================================================


class TestStateFingerprint:
    """engine._state_fingerprint 标量指纹测试。"""

    def test_scalar_keys_stable(self) -> None:
        from app.rag.engine import _state_fingerprint

        fp = _state_fingerprint(
            {"iteration": 2, "answer": "hi there", "retrieved_docs": [1, 2], "tool_results": [{}]}
        )
        assert fp == {"iteration": 2, "answer_len": 8, "retrieved_docs": 2, "tool_results": 1}
        assert list(fp.keys()) == ["iteration", "answer_len", "retrieved_docs", "tool_results"]

    def test_key_order_fixed_for_diff(self) -> None:
        from app.rag.engine import _state_fingerprint

        a = _state_fingerprint({"answer": "", "iteration": 0})
        b = _state_fingerprint({"answer": "ok", "iteration": 1})
        # 键序一致 → dict 可直接 diff
        assert list(a.keys()) == list(b.keys())

    def test_missing_keys_tolerated(self) -> None:
        from app.rag.engine import _state_fingerprint

        fp = _state_fingerprint({})  # 空状态 → 全 0/None，不抛异常
        assert fp == {"iteration": None, "answer_len": 0, "retrieved_docs": 0, "tool_results": 0}

    def test_none_fields_safe(self) -> None:
        from app.rag.engine import _state_fingerprint

        fp = _state_fingerprint({"answer": None, "retrieved_docs": None, "tool_results": None})
        assert fp["answer_len"] == 0
        assert fp["retrieved_docs"] == 0
        assert fp["tool_results"] == 0


# ======================================================================
# S1-S2 · generate span 差分埋点契约（源码文本锁定，不 import 引擎副作用）
# ======================================================================


class TestGenerateSpanDiffContract:
    """engine generate span 双向差分埋点 — 文本契约锁定。"""

    _ENGINE_SRC = __import__("os").path.join(
        __import__("os").path.dirname(__file__), "..", "app", "rag", "engine.py"
    )

    def _text(self) -> str:
        with open(__import__("os").path.normpath(self._ENGINE_SRC), encoding="utf-8") as f:
            return f.read()

    def test_generate_span_captures_state_before(self) -> None:
        text = self._text()
        assert "_state_before = _state_fingerprint(state)" in text

    def test_generate_span_captures_state_after(self) -> None:
        text = self._text()
        assert '"state_before": _state_before' in text
        assert '"state_after": _state_fingerprint(state)' in text


# ======================================================================
# S4-S8 · persist_tool_spans 三级降级
# ======================================================================


class TestPersistToolAuditComplete:
    """persist_tool_spans 补全（result_ref / result_full / evidence_ref）。"""

    def _make_span(
        self,
        span_type: str,
        name: str = "t",
        metadata: dict | None = None,
        output_ref: str = "result",
        evidence_ref: str | None = "ev_1",
    ) -> MagicMock:
        span = MagicMock()
        span.span_type = span_type
        span.name = name
        span.span_id = "span123"
        span.status = "ok"
        span.metadata = dict(metadata or {})
        span.output_ref = output_ref
        span.evidence_ref = evidence_ref
        span.error = None
        span.latency_ms = 12.0
        span.cost = {}
        return span

    @pytest.mark.asyncio
    async def test_s4_summary_truncated_ref_kept(self) -> None:
        from app.models.tool_audit import ToolAuditLog, persist_tool_spans

        added: list[ToolAuditLog] = []
        session = MagicMock()
        session.add.side_effect = added.append

        span = self._make_span(
            "tool.call",
            name="knowledge_search",
            metadata={"result_ref": "obj://bucket/kb_v1/asset_9"},
            output_ref="x" * 600,
        )
        await persist_tool_spans([span], session, run_id="r1", session_id="s1")
        record = added[0]
        assert len(record.result_summary) == 500
        assert record.result_ref == "obj://bucket/kb_v1/asset_9"
        assert record.evidence_ref == "ev_1"

    @pytest.mark.asyncio
    async def test_s5_no_ref_falls_back_null(self) -> None:
        from app.models.tool_audit import ToolAuditLog, persist_tool_spans

        added: list[ToolAuditLog] = []
        session = MagicMock()
        session.add.side_effect = added.append

        span = self._make_span(
            "tool.call", name="search", metadata={}, evidence_ref=None, output_ref="ok"
        )
        await persist_tool_spans([span], session, run_id="r", session_id="s")
        record = added[0]
        assert record.result_ref is None
        assert record.result_full is None
        assert record.evidence_ref is None
        assert record.result_summary == "ok"

    @pytest.mark.asyncio
    async def test_s6_key_event_writes_result_full(self) -> None:
        from app.models.tool_audit import ToolAuditLog, persist_tool_spans

        added: list[ToolAuditLog] = []
        session = MagicMock()
        session.add.side_effect = added.append

        # 关键事件 permission.decision：写 result_full 原文
        span = self._make_span(
            "permission.decision",
            name="permission:access",
            metadata={"result_full": {"decision": "approve", "rule_id": "r9"}},
        )
        await persist_tool_spans([span], session, run_id="r", session_id="s")
        record = added[0]
        assert record.result_full == {"decision": "approve", "rule_id": "r9"}
        # 普通 tool.call 不写 result_full
        assert record.result_ref is None

    @pytest.mark.asyncio
    async def test_s7_key_event_priority(self) -> None:
        from app.models.tool_audit import ToolAuditLog, persist_tool_spans

        added: list[ToolAuditLog] = []
        session = MagicMock()
        session.add.side_effect = added.append

        # 关键事件同时有 result_full 与 result_ref → 取 result_full
        span = self._make_span(
            "failure.recover",
            name="recover",
            metadata={
                "result_full": {"action": "rebuild"},
                "result_ref": "obj://bucket/fallback",
            },
        )
        await persist_tool_spans([span], session, run_id="r", session_id="s")
        record = added[0]
        assert record.result_full == {"action": "rebuild"}
        assert record.result_ref is None

    @pytest.mark.asyncio
    async def test_s7_normal_event_uses_result_ref_only(self) -> None:
        from app.models.tool_audit import ToolAuditLog, persist_tool_spans

        added: list[ToolAuditLog] = []
        session = MagicMock()
        session.add.side_effect = added.append

        span = self._make_span(
            "tool.call",
            metadata={"result_ref": "obj://bucket/x", "result_full": {"bad": True}},
        )
        await persist_tool_spans([span], session, run_id="r", session_id="s")
        record = added[0]
        assert record.result_ref == "obj://bucket/x"
        assert record.result_full is None

    @pytest.mark.asyncio
    async def test_arguments_exclude_big_fields(self) -> None:
        from app.models.tool_audit import ToolAuditLog, persist_tool_spans

        added: list[ToolAuditLog] = []
        session = MagicMock()
        session.add.side_effect = added.append

        span = self._make_span(
            "tool.call",
            metadata={
                "result_ref": "obj://bucket/x",
                "args_name": "kept",
                "result_full": {"big": True},
            },
        )
        await persist_tool_spans([span], session, run_id="r", session_id="s")
        record = added[0]
        assert "result_ref" not in record.arguments
        assert "result_full" not in record.arguments
        assert record.arguments.get("args_name") == "kept"