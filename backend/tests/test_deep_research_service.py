"""
Deep Research 服务测试 — P2-11。

覆盖范围：
    - 课题分解（正常/无子课题/失败回退）
    - 证据卡片（confirmed/uncertain/gap/LLM 失败）
    - 矛盾检测（检出/跳过空结论/无 detector）
    - 汇总摘要（LLM 摘要/全 gap/失败回退）
    - research 全流程（无 checkpoint + 断点恢复）
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.deep_research_service import (
    DeepResearchService,
    EvidenceCard,
    ResearchReport,
)


@pytest.fixture(autouse=True)
def _mock_celery(monkeypatch):
    """测试内隔离 celery，避免模块级 mock 污染 sys.modules 影响其它测试。"""
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    monkeypatch.setitem(sys.modules, "celery", mock_celery)
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    monkeypatch.setitem(sys.modules, "celery_app", mock_celery_app)


class _FakeLLM:
    """可控 Mock LLM — prompt 关键词 → 返回文本。"""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses

    async def chat(self, messages: list, stream: bool = True):
        prompt = messages[0].get("content", "")
        for keyword, text in self._responses.items():
            if keyword in prompt:
                yield text
                return
        yield "default"


class _FakeRetriever:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self._docs = docs or []

    async def search(self, query: str, kb_ids=None, top_k=5):
        return self._docs


class _FakeDetector:
    def __init__(self, contradiction: bool = False):
        self._contradiction = contradiction

    async def check_doc_contradiction(self, a: str, b: str):
        from app.context.contradiction_detector import ContradictionResult

        return ContradictionResult(
            has_contradiction=self._contradiction,
            description="contradiction" if self._contradiction else "",
            severity="medium" if self._contradiction else "low",
        )


# ======================================================================
# 课题分解
# ======================================================================


class TestDecomposeGoal:
    """课题分解测试。"""

    @pytest.mark.asyncio
    async def test_decomposes_into_topics(self) -> None:
        llm = _FakeLLM({"研究课题规划专家": "报销流程\n审批规则\n税务合规"})
        service = DeepResearchService(llm, _FakeRetriever())
        topics = await service._decompose_goal("公司报销合规调研")
        assert topics == ["报销流程", "审批规则", "税务合规"]

    @pytest.mark.asyncio
    async def test_single_topic_when_no_decomposition(self) -> None:
        llm = _FakeLLM({"研究课题规划专家": "公司报销合规调研"})
        service = DeepResearchService(llm, _FakeRetriever())
        topics = await service._decompose_goal("公司报销合规调研")
        assert topics == ["公司报销合规调研"]

    @pytest.mark.asyncio
    async def test_fallback_to_original_on_error(self) -> None:
        async def _failing_chat(messages, stream=True):
            raise RuntimeError("LLM down")
            yield ""

        llm = MagicMock()
        llm.chat = _failing_chat
        service = DeepResearchService(llm, _FakeRetriever())
        topics = await service._decompose_goal("目标")
        assert topics == ["目标"]


# ======================================================================
# 证据卡片
# ======================================================================


class TestGatherEvidence:
    """证据卡片测试。"""

    @pytest.mark.asyncio
    async def test_confirmed_with_docs(self) -> None:
        llm = _FakeLLM({"研究分析专家": '{"conclusion": "流程A → 审批 → 报销", "confidence": 0.85}'})
        retriever = _FakeRetriever([
            {"content": "流程A ...", "metadata": {"title": "报销流程"}, "score": 0.9},
        ])
        service = DeepResearchService(llm, retriever)
        card = await service._gather_evidence("报销流程", None)
        assert card.status == "confirmed"
        assert card.confidence == 0.85
        assert "流程A" in card.conclusion
        assert len(card.citations) == 1

    @pytest.mark.asyncio
    async def test_gap_when_no_docs(self) -> None:
        llm = _FakeLLM({})
        retriever = _FakeRetriever([])
        service = DeepResearchService(llm, retriever)
        card = await service._gather_evidence("未知主题", None)
        assert card.status == "gap"
        assert card.conclusion == ""
        assert card.citations == []

    @pytest.mark.asyncio
    async def test_uncertain_with_low_confidence(self) -> None:
        llm = _FakeLLM({"研究分析专家": '{"conclusion": "可能B", "confidence": 0.5}'})
        retriever = _FakeRetriever([
            {"content": "模糊信息", "metadata": {}, "score": 0.4},
        ])
        service = DeepResearchService(llm, retriever)
        card = await service._gather_evidence("报销流程", None)
        assert card.status == "uncertain"
        assert card.confidence == 0.5


# ======================================================================
# 矛盾检测
# ======================================================================


class TestDetectContradictions:
    """跨课题矛盾检测测试。"""

    @pytest.mark.asyncio
    async def test_no_detector_returns_empty(self) -> None:
        service = DeepResearchService(_FakeLLM({}), _FakeRetriever(), None)
        result = await service._detect_contradictions([])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_contradiction(self) -> None:
        service = DeepResearchService(
            _FakeLLM({}), _FakeRetriever(), _FakeDetector(False)
        )
        cards = [
            EvidenceCard("A", conclusion="x"),
            EvidenceCard("B", conclusion="y"),
        ]
        result = await service._detect_contradictions(cards)
        assert result == []

    @pytest.mark.asyncio
    async def test_detects_contradiction(self) -> None:
        service = DeepResearchService(
            _FakeLLM({}), _FakeRetriever(), _FakeDetector(True)
        )
        cards = [
            EvidenceCard("A", conclusion="x"),
            EvidenceCard("B", conclusion="y"),
        ]
        result = await service._detect_contradictions(cards)
        assert len(result) == 1
        assert result[0]["topic_a"] == "A"
        assert result[0]["severity"] == "medium"

    @pytest.mark.asyncio
    async def test_skips_empty_conclusions(self) -> None:
        service = DeepResearchService(
            _FakeLLM({}), _FakeRetriever(), _FakeDetector(True)
        )
        cards = [
            EvidenceCard("A", conclusion=""),
            EvidenceCard("B", conclusion="y"),
        ]
        result = await service._detect_contradictions(cards)
        assert result == []


# ======================================================================
# 汇总摘要
# ======================================================================


class TestSummarize:
    """摘要测试。"""

    @pytest.mark.asyncio
    async def test_summary_from_conclusions(self) -> None:
        llm = _FakeLLM({"研究报告撰写专家": "报销合规，审批规则清晰"})
        service = DeepResearchService(llm, _FakeRetriever())
        cards = [
            EvidenceCard("A", conclusion="流程OK", status="confirmed"),
        ]
        summary = await service._summarize("公司报销合规", cards)
        assert "报销合规" in summary

    @pytest.mark.asyncio
    async def test_all_gap_fallback(self) -> None:
        service = DeepResearchService(_FakeLLM({}), _FakeRetriever())
        cards = [EvidenceCard("A", status="gap")]
        summary = await service._summarize("合规", cards)
        assert "缺口" in summary

    @pytest.mark.asyncio
    async def test_llm_failure_uses_rule_summary(self) -> None:
        async def _failing_chat(messages, stream=True):
            raise RuntimeError("LLM down")
            yield ""

        llm = MagicMock()
        llm.chat = _failing_chat
        service = DeepResearchService(llm, _FakeRetriever())
        cards = [EvidenceCard("A", conclusion="结论A", status="confirmed")]
        summary = await service._summarize("合规", cards)
        assert "结论A" in summary


# ======================================================================
# 全流程
# ======================================================================


class TestResearch:
    """research() 主入口测试。"""

    @pytest.mark.asyncio
    async def test_research_without_checkpoint(self) -> None:
        llm = _FakeLLM({
            "研究课题规划专家": "流程\n审批\n合规",
            "研究分析专家": '{"conclusion": "结论A", "confidence": 0.9}',
            "研究报告撰写专家": "摘要",
        })
        retriever = _FakeRetriever([
            {"content": "doc1", "metadata": {"title": "T1"}, "score": 0.8},
        ])
        service = DeepResearchService(llm, retriever, _FakeDetector(False))
        report = await service.research("公司报销合规调研")
        assert isinstance(report, ResearchReport)
        assert len(report.topics) == 3
        assert len(report.cards) == 3
        assert report.confidence_distribution["confirmed"] == 3
        assert report.summary

    @pytest.mark.asyncio
    async def test_research_with_checkpoint_resume(self) -> None:
        """里程碑断点恢复：已有 done 里程碑跳过已完成子课题。"""
        llm = _FakeLLM({
            "研究课题规划专家": "流程\n审批",
            "研究分析专家": '{"conclusion": "结论", "confidence": 0.9}',
            "研究报告撰写专家": "摘要",
        })
        retriever = _FakeRetriever([
            {"content": "doc", "metadata": {}, "score": 0.8},
        ])
        service = DeepResearchService(llm, retriever, _FakeDetector(False))

        class FakeMgr:
            async def save_milestone(self, key, name, detail=None, **kwargs):
                pass

            async def get_milestones(self, key):
                return [
                    {
                        "name": "topic_0",
                        "detail": {
                            "status": "done",
                            "result": EvidenceCard(
                                "流程", "结论A", confidence=0.9, status="confirmed"
                            ).to_dict(),
                        },
                    },
                ]

        report = await service.research(
            "调研", checkpoint_manager=FakeMgr(), task_id="t1"
        )
        assert len(report.cards) == 2
        assert report.cards[0].conclusion == "结论A"
        assert report.cards[0].status == "confirmed"
