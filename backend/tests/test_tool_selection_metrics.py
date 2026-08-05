"""工具选择准确度评测测试 — P1-4 标注式工具选择评测维度。

覆盖范围：
    - tool_selection_metrics.py：extract_called_tools / compute / aggregate
    - dataset.py：expected_tools / forbidden_tools 字段加载与指纹
    - runner.py 集成：标注 case 经 Span 证据计算工具选择分数
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
# extract_called_tools
# ======================================================================


class TestExtractCalledTools:
    """从 Span 证据提取实际调用工具名测试。"""

    def test_extract_from_audit_spans(self) -> None:
        from app.eval.tool_selection_metrics import extract_called_tools

        spans = [
            {"span_type": "tool.call", "name": "tool:knowledge_search"},
            {"span_type": "tool.call", "name": "tool:document_get"},
            {"span_type": "tool.call", "name": "tool:knowledge_search"},  # 重复
        ]
        called = extract_called_tools(spans)
        assert called == ["knowledge_search", "document_get"]

    def test_ignores_node_spans(self) -> None:
        """LangGraph 节点 Span（name=tool_call_iter{N}）不应被识别为工具调用。"""
        from app.eval.tool_selection_metrics import extract_called_tools

        spans = [
            {"span_type": "tool.call", "name": "tool_call_iter1"},  # 节点 Span
            {"span_type": "tool.call", "name": "tool:knowledge_search"},
        ]
        called = extract_called_tools(spans)
        assert called == ["knowledge_search"]

    def test_ignores_non_tool_call_spans(self) -> None:
        from app.eval.tool_selection_metrics import extract_called_tools

        spans = [
            {"span_type": "context.load", "name": "tool:knowledge_search"},
            {"span_type": "plan.create", "name": "think"},
        ]
        assert extract_called_tools(spans) == []

    def test_empty_spans(self) -> None:
        from app.eval.tool_selection_metrics import extract_called_tools

        assert extract_called_tools([]) == []


# ======================================================================
# compute_tool_selection_metrics
# ======================================================================


class TestComputeToolSelectionMetrics:
    """工具选择准确度指标计算测试。"""

    def test_none_when_no_annotations(self) -> None:
        from app.eval.tool_selection_metrics import compute_tool_selection_metrics

        assert compute_tool_selection_metrics(["a"], None, None) is None
        assert compute_tool_selection_metrics(["a"], [], []) is None

    def test_perfect_match(self) -> None:
        from app.eval.tool_selection_metrics import compute_tool_selection_metrics

        r = compute_tool_selection_metrics(
            ["knowledge_search"], ["knowledge_search"], None
        )
        assert r is not None
        assert r["precision"] == 1.0
        assert r["recall"] == 1.0
        assert r["f1"] == 1.0
        assert r["expected_missing"] == []
        assert r["forbidden_called"] == []
        assert r["passed"] is True

    def test_missing_expected_tool(self) -> None:
        """应调未调 → recall 不足。"""
        from app.eval.tool_selection_metrics import compute_tool_selection_metrics

        r = compute_tool_selection_metrics(
            [], ["knowledge_search"], None
        )
        assert r is not None
        assert r["recall"] == 0.0
        assert r["precision"] == 0.0
        assert r["expected_missing"] == ["knowledge_search"]
        assert r["passed"] is False

    def test_extra_tool_lowers_precision(self) -> None:
        """多调了非期望工具 → precision 下降。"""
        from app.eval.tool_selection_metrics import compute_tool_selection_metrics

        r = compute_tool_selection_metrics(
            ["knowledge_search", "document_get"], ["knowledge_search"], None
        )
        assert r is not None
        assert r["recall"] == 1.0
        assert r["precision"] == 0.5  # 1 正确 / 2 调用
        assert r["passed"] is True  # 无 missing / 无 forbidden

    def test_forbidden_tool_called_fails(self) -> None:
        """调用了禁止工具 → passed 为 False。"""
        from app.eval.tool_selection_metrics import compute_tool_selection_metrics

        r = compute_tool_selection_metrics(
            ["knowledge_search", "document_create"],
            ["knowledge_search"],
            ["document_create"],
        )
        assert r is not None
        assert r["forbidden_called"] == ["document_create"]
        # document_create 非期望 → precision 下降
        assert r["precision"] == 0.5
        assert r["passed"] is False

    def test_only_forbidden_annotation(self) -> None:
        """仅有 forbidden 标注（负样本）：未调用 → 满分通过。"""
        from app.eval.tool_selection_metrics import compute_tool_selection_metrics

        r = compute_tool_selection_metrics([], None, ["document_create"])
        assert r is not None
        assert r["recall"] == 1.0  # 无 expected 视为不适用满分
        assert r["precision"] == 1.0  # 无调用
        assert r["passed"] is True

    def test_only_forbidden_annotation_violated(self) -> None:
        """仅有 forbidden 标注：调用了禁止工具 → 不通过。"""
        from app.eval.tool_selection_metrics import compute_tool_selection_metrics

        r = compute_tool_selection_metrics(
            ["document_create"], None, ["document_create"]
        )
        assert r is not None
        assert r["forbidden_called"] == ["document_create"]
        assert r["passed"] is False


# ======================================================================
# aggregate_tool_selection_metrics
# ======================================================================


class TestAggregateToolSelectionMetrics:
    """run 级聚合测试。"""

    def test_empty_returns_empty(self) -> None:
        from app.eval.tool_selection_metrics import aggregate_tool_selection_metrics

        assert aggregate_tool_selection_metrics([]) == {}

    def test_aggregate(self) -> None:
        from app.eval.tool_selection_metrics import aggregate_tool_selection_metrics

        case_metrics = [
            {"precision": 1.0, "recall": 1.0, "f1": 1.0, "passed": True},
            {"precision": 0.5, "recall": 1.0, "f1": 0.6667, "passed": True},
            {"precision": 0.0, "recall": 0.0, "f1": 0.0, "passed": False},
        ]
        agg = aggregate_tool_selection_metrics(case_metrics)
        assert agg["tool_selection_case_count"] == 3
        assert agg["avg_tool_precision"] == round((1.0 + 0.5 + 0.0) / 3, 4)
        assert agg["tool_selection_pass_rate"] == round(2 / 3, 4)


# ======================================================================
# EvalCase 字段加载
# ======================================================================


class TestEvalCaseToolFields:
    """EvalCase expected_tools / forbidden_tools 加载。"""

    def test_from_dict_with_tools(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase.from_dict(
            {
                "query": "查我的 OA 请假记录",
                "expected_doc_ids": [],
                "expected_tools": ["knowledge_search", "oa_query"],
                "forbidden_tools": ["document_create"],
            }
        )
        assert case.expected_tools == ["knowledge_search", "oa_query"]
        assert case.forbidden_tools == ["document_create"]

    def test_from_dict_without_tools(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase.from_dict({"query": "q", "expected_doc_ids": []})
        assert case.expected_tools == []
        assert case.forbidden_tools == []

    def test_to_dict_roundtrip(self) -> None:
        from app.eval.dataset import EvalCase

        case = EvalCase(
            query="q",
            expected_doc_ids=[],
            expected_tools=["knowledge_search"],
            forbidden_tools=["document_create"],
        )
        restored = EvalCase.from_dict(case.to_dict())
        assert restored.expected_tools == ["knowledge_search"]
        assert restored.forbidden_tools == ["document_create"]

    def test_fingerprint_changes_with_tools(self) -> None:
        """expected_tools 变更应改变数据集指纹。"""
        from app.eval.dataset import EvalCase, EvalDataset

        ds1 = EvalDataset([EvalCase(query="q", expected_doc_ids=[], expected_tools=["a"])])
        ds2 = EvalDataset([EvalCase(query="q", expected_doc_ids=[], expected_tools=["b"])])
        assert ds1.fingerprint() != ds2.fingerprint()


# ======================================================================
# Runner 集成
# ======================================================================


def _make_engine_with_tool_spans(tool_names: list[str]) -> MagicMock:
    """构造 mock engine：检索时发出携带 tool.call 审计 Span 的 TraceContext。"""
    from app.observability.langfuse_tracer import TraceContext

    engine = MagicMock()

    async def fake_retrieve(state: dict, kb_ids: object = None) -> None:
        ctx = TraceContext(session_id=state.get("session_id", ""))
        for tn in tool_names:
            ctx.span(name=f"tool:{tn}", span_type="tool.call")
        state["retrieved_docs"] = []

    engine._retrieve = fake_retrieve
    return engine


class TestRunnerToolSelectionIntegration:
    """EvalRunner 工具选择评分集成。"""

    @pytest.mark.asyncio
    async def test_tool_selection_perfect_match(self) -> None:
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = _make_engine_with_tool_spans(["knowledge_search"])
        runner = EvalRunner(engine=engine)
        case = EvalCase(
            query="查我的请假记录",
            expected_doc_ids=[],
            expected_tools=["knowledge_search"],
        )
        result = await runner.run([case], with_generation=False)
        cr = result.case_results[0]
        assert cr.tool_selection_metrics is not None
        assert cr.tool_selection_metrics["recall"] == 1.0
        assert cr.tool_selection_metrics["precision"] == 1.0
        assert cr.tool_selection_metrics["passed"] is True
        assert cr.passed is True
        # run 级聚合
        assert result.tool_selection_summary["tool_selection_case_count"] == 1
        assert result.tool_selection_summary["avg_tool_f1"] == 1.0

    @pytest.mark.asyncio
    async def test_tool_selection_missing_fails_case(self) -> None:
        """应调 knowledge_search 但未调用 → recall 0 → 用例不通过。"""
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = _make_engine_with_tool_spans([])  # 未调用任何工具
        runner = EvalRunner(engine=engine)
        case = EvalCase(
            query="查我的请假记录",
            expected_doc_ids=[],
            expected_tools=["knowledge_search"],
        )
        result = await runner.run([case], with_generation=False)
        cr = result.case_results[0]
        assert cr.tool_selection_metrics is not None
        assert cr.tool_selection_metrics["recall"] == 0.0
        assert cr.tool_selection_metrics["passed"] is False
        assert cr.passed is False

    @pytest.mark.asyncio
    async def test_no_annotation_skips_metric(self) -> None:
        """无 expected_tools/forbidden_tools 标注 → tool_selection_metrics 为 None。"""
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = _make_engine_with_tool_spans(["knowledge_search"])
        runner = EvalRunner(engine=engine)
        case = EvalCase(query="普通问题", expected_doc_ids=[])
        result = await runner.run([case], with_generation=False)
        cr = result.case_results[0]
        assert cr.tool_selection_metrics is None
        assert result.tool_selection_summary == {}

    @pytest.mark.asyncio
    async def test_forbidden_tool_called_fails(self) -> None:
        """调用了禁止工具 → passed False。"""
        from app.eval.dataset import EvalCase
        from app.eval.runner import EvalRunner

        engine = _make_engine_with_tool_spans(
            ["knowledge_search", "document_create"]
        )
        runner = EvalRunner(engine=engine)
        case = EvalCase(
            query="查记录",
            expected_doc_ids=[],
            expected_tools=["knowledge_search"],
            forbidden_tools=["document_create"],
        )
        result = await runner.run([case], with_generation=False)
        cr = result.case_results[0]
        assert cr.tool_selection_metrics is not None
        assert cr.tool_selection_metrics["forbidden_called"] == ["document_create"]
        assert cr.tool_selection_metrics["passed"] is False
        assert cr.passed is False
