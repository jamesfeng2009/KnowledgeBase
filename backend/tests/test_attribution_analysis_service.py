"""
T4 归因分析接线测试 — app/services/attribution_analysis_service.py。

对齐 task tracking T4 验收标准：
    1. 有数据支撑的归因 + 结论可点回原始 decision_id（事实锚点）
    2. 沉淀只进 knowledge_candidates 走审批，不污染正式知识库；
       负对照不过 / 无数据一律拒绝沉淀
    3. 负对照测试确保分析管道自身不制造效应

核心原则：**没有事件数据就诚实返回 no_data，绝不编造归因**。
"""

import random
from unittest.mock import AsyncMock

import pytest

from app.services.attribution_analysis_service import (
    ATTRIBUTION_SOURCE_TYPE,
    AttributionAnalysisService,
)


def _dec(i, arm, urgency="low", **kw):
    return {
        "decision_id": f"d{i}",
        "experiment": "exp",
        "arm": arm,
        "unit_id": f"u{i}",
        "strata": {"urgency": urgency},
        **kw,
    }


def _out(i, filled, **kw):
    return {
        "decision_id": f"d{i}",
        "filled": filled,
        "responded": filled,
        "retained_30d": filled,
        "source_system": "test",
        **kw,
    }


def _clean_events(n_per_arm=120, effect=0.0, seed=7):
    """构造无混杂、高匹配率的干净事件；effect 控制 treatment 的额外提升。"""
    rng = random.Random(seed)
    decisions, outcomes = [], []
    idx = 0
    for arm in ("control", "treatment"):
        base = 0.30 + (effect if arm == "treatment" else 0.0)
        for _ in range(n_per_arm):
            decisions.append(_dec(idx, arm))
            outcomes.append(_out(idx, filled=rng.random() < base))
            idx += 1
    return decisions, outcomes


class TestNoData门禁:
    """最关键的诚实性：没有真实事件时绝不编造归因。"""

    def test_empty_inputs_return_no_data(self):
        result = AttributionAnalysisService().analyze([], [])
        assert result["status"] == "no_data"
        assert result["trustworthy"] is False
        assert result["report"] is None
        assert result["evidence_decision_ids"] == []
        assert any("数据源" in w or "未接入" in w for w in result["warnings"])

    def test_only_decisions_no_outcomes_is_no_data(self):
        result = AttributionAnalysisService().analyze([_dec(1, "control")], [])
        assert result["status"] == "no_data"

    def test_no_data_never_promotes(self):
        pool = AsyncMock()
        result = AttributionAnalysisService().analyze([], [])
        skipped = __import__("asyncio").run(
            AttributionAnalysisService().promote_to_candidate(pool, result)
        )
        assert skipped["status"] == "skipped"
        assert skipped["reason"] == "no_data"
        pool.upsert_from_signal.assert_not_called()


class TestNegativeControlGate:
    """A/A 负对照不过时，结论必须标不可信。"""

    def test_null_effect_passes_and_is_trustworthy(self):
        decisions, outcomes = _clean_events(effect=0.0)
        result = AttributionAnalysisService().analyze(decisions, outcomes)
        assert result["status"] == "ok"
        assert result["negative_control"]["passed"] is True
        assert result["trustworthy"] is True

    def test_negative_control_disabled_is_untrustworthy(self):
        decisions, outcomes = _clean_events(effect=0.0)
        result = AttributionAnalysisService().analyze(
            decisions, outcomes, run_negative_control=False
        )
        assert result["status"] == "ok"
        assert result["negative_control"]["passed"] is False
        assert result["trustworthy"] is False
        assert any("负对照" in w for w in result["warnings"])

    def test_insufficient_sample_is_untrustworthy(self):
        # 每臂 10 条 < A/A 所需 40 条 → 负对照无法执行
        decisions, outcomes = _clean_events(n_per_arm=10)
        result = AttributionAnalysisService().analyze(decisions, outcomes, min_samples=5)
        assert result["negative_control"]["passed"] is False
        assert "样本不足" in result["negative_control"]["reason"]
        assert result["trustworthy"] is False

    def test_bad_negative_control_warning_present(self):
        decisions, outcomes = _clean_events(n_per_arm=10)
        result = AttributionAnalysisService().analyze(decisions, outcomes, min_samples=5)
        assert any("A/A 负对照未通过" in w for w in result["warnings"])


class TestEvidenceAnchors:
    """每条结论都要能点回原始 decision_id。"""

    def test_evidence_ids_are_returned(self):
        decisions, outcomes = _clean_events(n_per_arm=60)
        result = AttributionAnalysisService().analyze(decisions, outcomes)
        assert result["evidence_decision_ids"]
        assert all(isinstance(x, str) for x in result["evidence_decision_ids"])

    def test_unmatched_decisions_not_used_as_evidence(self):
        decisions, outcomes = _clean_events(n_per_arm=60)
        # 人为丢掉一半结果 → 匹配率跌破 0.8
        outcomes = outcomes[:60]
        result = AttributionAnalysisService().analyze(decisions, outcomes)
        matched = result["report"]["diagnostics"]["matched"]
        assert len(result["evidence_decision_ids"]) == min(matched, len(decisions))
        assert any("匹配率" in w for w in result["warnings"])
        assert result["trustworthy"] is False

    def test_answer_draft_includes_anchors(self):
        decisions, outcomes = _clean_events(n_per_arm=60)
        result = AttributionAnalysisService().analyze(decisions, outcomes)
        draft = AttributionAnalysisService._render_answer_draft(result)
        assert "decision_id" in draft
        assert "d0" in draft


class TestPromotionGate:
    """沉淀只进候选池，且必须通过所有门禁。"""

    @pytest.mark.asyncio
    async def test_trustworthy_result_promotes_to_candidate_pool(self):
        decisions, outcomes = _clean_events(n_per_arm=120)
        result = AttributionAnalysisService().analyze(decisions, outcomes)
        assert result["trustworthy"] is True

        pool = AsyncMock()
        pool.upsert_from_signal.return_value = {
            "status": "queued",
            "candidate_id": "cand-1",
        }
        out = await AttributionAnalysisService().promote_to_candidate(
            pool, result, user_id="u-1"
        )
        assert out["status"] == "queued"
        pool.upsert_from_signal.assert_awaited_once()
        kwargs = pool.upsert_from_signal.call_args.kwargs
        assert kwargs["source_type"] == ATTRIBUTION_SOURCE_TYPE
        assert kwargs["answer_draft"]
        assert "decision_id" in kwargs["answer_draft"]
        # 用户 ID 透传，供后续归簇/审批使用
        assert kwargs["user_id"] == "u-1"

    @pytest.mark.asyncio
    async def test_untrustworthy_result_never_promotes(self):
        decisions, outcomes = _clean_events(n_per_arm=10)
        result = AttributionAnalysisService().analyze(decisions, outcomes, min_samples=5)
        pool = AsyncMock()
        out = await AttributionAnalysisService().promote_to_candidate(pool, result)
        assert out["reason"] == "untrustworthy"
        pool.upsert_from_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_key_is_stable(self):
        decisions, outcomes = _clean_events(n_per_arm=120)
        result = AttributionAnalysisService().analyze(decisions, outcomes)
        pool = AsyncMock()
        pool.upsert_from_signal.return_value = {"status": "queued"}
        svc = AttributionAnalysisService()
        await svc.promote_to_candidate(pool, result)
        first_id = pool.upsert_from_signal.call_args.kwargs["source_id"]
        pool.reset_mock()
        await svc.promote_to_candidate(pool, result)
        second_id = pool.upsert_from_signal.call_args.kwargs["source_id"]
        assert first_id == second_id == result["evidence_decision_ids"][0]


class TestMCPTool:
    """工具注册 + 无数据诚实返回。"""

    def test_tool_is_registered(self):
        import sys
        from unittest.mock import MagicMock

        for m in ["celery", "celery_app", "opensearchpy", "pymilvus"]:
            if m not in sys.modules:
                sys.modules[m] = MagicMock()
        from app.mcp.server import KnowledgeBaseMCPServer

        server = KnowledgeBaseMCPServer(db_factory=MagicMock())
        tools = __import__("asyncio").run(server.list_tools())
        # Tool 是 TypedDict，list_tools 返回 dict 列表
        names = [t["name"] if isinstance(t, dict) else t.name for t in tools]
        assert "analyze_outcome_attribution" in names

    def test_tool_returns_no_data_by_default(self):
        import asyncio
        import json
        import sys
        from unittest.mock import MagicMock

        for m in ["celery", "celery_app", "opensearchpy", "pymilvus"]:
            if m not in sys.modules:
                sys.modules[m] = MagicMock()
        from app.mcp.server import KnowledgeBaseMCPServer

        server = KnowledgeBaseMCPServer(db_factory=MagicMock())
        raw = asyncio.run(
            server.call_tool("analyze_outcome_attribution", {})
        )
        payload = json.loads(raw)
        assert payload["status"] == "no_data"
        assert payload["evidence_decision_ids"] == []

    def test_tool_is_in_safe_tools(self):
        from app.rag.tool_guard import _SAFE_TOOLS

        assert "analyze_outcome_attribution" in _SAFE_TOOLS
