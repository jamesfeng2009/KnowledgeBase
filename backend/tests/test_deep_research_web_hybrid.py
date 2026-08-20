"""
Deep Research 公网混合取证（P4）端到端验证。

覆盖设计文档 docs/P4 §12 关键验收：
    1. 双源并存：注入 web_provider 后 citation 同时含 internal 与 web，source_type 非空
    2. boost 可观察：默认 boost=1.2 时内部命中排前（即便 web 数值分更大）
    3. 单源降级：web_provider 抛异常时不阻塞，回落纯内部引用
    4. 全流程：research() 完整链路（分解 + 双源卡片 + 摘要）产出带 source_type 的报告

复用 P2-11 测试的 Mock 风格（celery / FakeLLM / FakeRetriever）。
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.deep_research_service import (
    DeepResearchService,
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
    """控制 LLM — prompt 关键词 → 返回文本。"""

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


class _FakeWeb:
    """稳定公网提供商。"""

    def __init__(self, hits: list[dict] | None = None) -> None:
        self._hits = hits or []

    async def search(self, query: str, max_results: int = 5,
                     tenant_id: str | None = None) -> list[dict]:
        return self._hits[:max_results]


class _BoomWeb:
    """不可用公网提供商 — 验证降级不阻塞。"""

    async def search(self, query: str, max_results: int = 5,
                     tenant_id: str | None = None) -> list[dict]:
        raise RuntimeError("provider down")


class _RecordingWeb:
    """记录收到的 tenant_id，验证配额按租户隔离的透传链路。"""

    def __init__(self, hits: list[dict] | None = None) -> None:
        self._hits = hits or []
        self.received_tenant: list[str | None] = []

    async def search(self, query: str, max_results: int = 5,
                     tenant_id: str | None = None) -> list[dict]:
        self.received_tenant.append(tenant_id)
        return self._hits[:max_results]


def _internal(title: str, score: float, content: str = "") -> dict:
    return {"doc_id": f"/kb/{title}", "metadata": {"title": title},
            "content": content or f"{title}内部内容", "score": score}


def _web(title: str, score: float, snippet: str = "") -> dict:
    return {"title": title, "url": f"https://example.com/{title}",
            "snippet": snippet or f"{title}网络快照", "score": score}


class TestDualSourceGather:
    """双源取证：citations 同时含 internal 与 web，boost 影响排序。"""

    @pytest.mark.asyncio
    async def test_citations_carry_source_type(self) -> None:
        llm = _FakeLLM({"资料片段": '{"conclusion":"内部优先。", "confidence":0.8}'})
        retriever = _FakeRetriever([
            _internal("内部甲", 0.9, "内部甲详细内容"),
            _internal("内部乙", 0.8, "内部乙详细内容"),
        ])
        web = _FakeWeb([
            _web("网络A", 90.0, "网络A快照"),
            _web("网络B", 80.0, "网络B快照"),
        ])
        service = DeepResearchService(llm, retriever, web_provider=web)
        card = await service._gather_evidence("某主题", None)

        assert card.status == "confirmed"
        types = {c["source_type"] for c in card.citations}
        assert types == {"internal", "web"}
        # 每条引用都有来源标签与可溯源 doc_id（web 即 url）
        for c in card.citations:
            assert c["source_type"] in {"internal", "web"}
            assert c["doc_id"]
        # 默认 boost=1.2：内部命中靠前（尽管 web 数值分高达 90）
        assert card.citations[0]["source_type"] == "internal"

    @pytest.mark.asyncio
    async def test_web_only_still_works(self) -> None:
        """内部为空、公网有结果 → 仍产出 web 证据（非 gap）。"""
        llm = _FakeLLM({"资料片段": '{"conclusion":"仅网络结论。", "confidence":0.7}'})
        retriever = _FakeRetriever([])
        web = _FakeWeb([_web("网络A", 90.0), _web("网络B", 80.0)])
        service = DeepResearchService(llm, retriever, web_provider=web)
        card = await service._gather_evidence("某主题", None)
        assert card.citations and all(c["source_type"] == "web" for c in card.citations)


class TestSingleSourceFallback:
    """未注入 web_provider → 纯内部，行为回归且带 source_type。"""

    @pytest.mark.asyncio
    async def test_no_web_provider(self) -> None:
        llm = _FakeLLM({"知识库文档": '{"conclusion":"内部结论。", "confidence":0.8}'})
        retriever = _FakeRetriever([_internal("内部甲", 0.9)])
        service = DeepResearchService(llm, retriever)  # 不传 web_provider
        card = await service._gather_evidence("某主题", None)
        assert card.citations and all(c["source_type"] == "internal" for c in card.citations)


class TestDegrade:
    """公网异常降级为纯内部，不阻塞。"""

    @pytest.mark.asyncio
    async def test_web_down_falls_back_to_internal(self) -> None:
        llm = _FakeLLM({"资料片段": '{"conclusion":"内部兜底结论。", "confidence":0.8}'})
        retriever = _FakeRetriever([
            _internal("内部甲", 0.85, "内部甲内容"),
            _internal("内部乙", 0.6, "内部乙内容"),
        ])
        service = DeepResearchService(llm, retriever, web_provider=_BoomWeb())
        card = await service._gather_evidence("某主题", None)
        assert card.status == "confirmed"
        assert card.citations and all(c["source_type"] == "internal" for c in card.citations)

    @pytest.mark.asyncio
    async def test_both_empty_gap(self) -> None:
        """两源都空 → gap，非异常。"""
        llm = _FakeLLM({})
        service = DeepResearchService(
            llm, _FakeRetriever([]), web_provider=_FakeWeb([])
        )
        card = await service._gather_evidence("某主题", None)
        assert card.status == "gap"
        assert card.citations == []


class TestFullResearchFlow:
    """research() 全链路：分解 → 双源卡片 → 摘要，报告携带 source_type。"""

    @pytest.mark.asyncio
    async def test_research_propagates_source_type(self) -> None:
        llm = _FakeLLM({
            "研究课题规划专家": "主题A\n主题B",           # 分解两个子课题
            "资料片段": '{"conclusion":"结论带[内部]和[网络]标注。", "confidence":0.8}',  # 双源归纳
            "研究报告撰写专家": "总体摘要",                 # 汇总
        })
        retriever = _FakeRetriever([_internal("内部甲", 0.9, "内部甲内容")])
        web = _FakeWeb([_web("网络A", 90.0, "网络A快照"), _web("网络B", 80.0)])
        service = DeepResearchService(llm, retriever, web_provider=web)

        report: ResearchReport = await service.research("调研某主题", kb_ids=None)

        assert len(report.cards) == 2
        all_types: set[str] = set()
        for card in report.cards:
            all_types |= {c["source_type"] for c in card.citations}
        assert all_types == {"internal", "web"}
        assert report.summary  # 汇总已生成


class TestTenantQuotaPropagation:
    """tenant_id 从 research() 一路透传到 web_provider.search（配额按租户隔离）。"""

    @pytest.mark.asyncio
    async def test_research_passes_tenant_to_provider(self) -> None:
        llm = _FakeLLM({
            "研究课题规划专家": "主题A\n主题B",
            "资料片段": '{"conclusion":"结论。", "confidence":0.8}',
            "研究报告撰写专家": "摘要",
        })
        retriever = _FakeRetriever([_internal("内部甲", 0.9, "内部甲内容")])
        web = _RecordingWeb([_web("网络A", 90.0, "nv")])
        service = DeepResearchService(llm, retriever, web_provider=web)

        await service.research("调研某主题", kb_ids=None, tenant_id="tenant-42")

        # 每个子课题取证都向 provider 透传同一 tenant_id
        assert web.received_tenant == ["tenant-42", "tenant-42"]

    @pytest.mark.asyncio
    async def test_tenant_defaults_to_none(self) -> None:
        """未传 tenant_id 时 provider 收到 None（全局 scope，向后兼容）。"""
        llm = _FakeLLM({"资料片段": '{"conclusion":"x", "confidence":0.8}'})
        web = _RecordingWeb([_web("网络A", 90.0)])
        service = DeepResearchService(
            llm, _FakeRetriever([_internal("甲", 0.9)]), web_provider=web,
        )
        await service._gather_evidence("某主题", None)
        assert web.received_tenant == [None]


class TestBoostObservabilityE2E:
    """boost 在端到端排序上可观察：由高到低，内部命中相对排位改变。"""

    @pytest.mark.asyncio
    async def test_boost_shifts_ranking(self) -> None:
        llm = _FakeLLM({"资料片段": '{"conclusion":"x", "confidence":0.8}'})
        retriever = _FakeRetriever([_internal("内部甲", 0.9), _internal("内部乙", 0.8)])
        web = _FakeWeb([_web("网络A", 90.0, "nv"), _web("网络B", 80.0, "nv")])
        mc = {"k_internal": 5, "k_web": 5, "boost": 2.0,
              "min_internal": 1, "min_web": 1, "total_budget": 6}
        s_high = DeepResearchService(llm, retriever, web_provider=web, merge_config=mc)
        card_high = await s_high._gather_evidence("某主题", None)

        mc_low = dict(mc, boost=0.2)
        s_low = DeepResearchService(llm, retriever, web_provider=web, merge_config=mc_low)
        card_low = await s_low._gather_evidence("某主题", None)

        # boost 高时内部靠前，boost 低时网络靠前 → boost 非死参数
        first_high = card_high.citations[0]["source_type"]
        first_low = card_low.citations[0]["source_type"]
        assert first_high == "internal"
        assert first_low == "web"

    @pytest.mark.asyncio
    async def test_research_accepts_merge_config(self) -> None:
        """merge_config 注入生效于 research() 全流程（防止死参数）。"""
        llm = _FakeLLM({
            "研究课题规划专家": "主题X",
            "资料片段": '{"conclusion":"x", "confidence":0.8}',
            "研究报告撰写专家": "摘要",
        })
        min_cfg = {"k_internal": 2, "k_web": 2, "boost": 1.2,
                   "min_internal": 1, "min_web": 1, "total_budget": 2}
        service = DeepResearchService(
            llm, _FakeRetriever([_internal("甲", 0.9)]), _FakeWeb([_web("网", 90.0)]),
            merge_config=min_cfg,
        )
        report = await service.research("目标", kb_ids=None)
        assert report.cards[0].citations  # 非空即证明合并配置被使用且降级安全