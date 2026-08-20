"""P2-B Task 4: engine 集成 + SSE 事件 测试。

测试覆盖：
    1. QueryRewriter 注入到 engine
    2. answer() 发出 QUERY_REWRITE SSE 事件
    3. 重写后的查询用于检索
    4. 原始查询用于重排和生成
    5. QueryRewriter 为 None 时不影响正常流程
    6. QueryRewriter 失败时降级
    7. 日志输出验证
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest

from app.rag.query_rewriter import QueryRewriteResult
from app.utils.sse import SSEEvent, SSEEventType
from tests.test_rag_engine import (
    FakeGenerator,
    FakeLLM,
    FakeMCPClient,
    FakeReranker,
    FakeRetriever,
    _make_engine,
)


# ======================================================================
# 多响应 FakeLLM — 先 retrieve 再 generate
# ======================================================================


class MultiResponseLLM:
    """Mock LLM — 按顺序返回预设响应。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self._index = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        if self._index < len(self.responses):
            resp = self.responses[self._index]
            self._index += 1
        else:
            resp = "generate"
        yield resp


# ======================================================================
# Mock QueryRewriter
# ======================================================================


class FakeQueryRewriter:
    """Mock QueryRewriter — 返回预设的 QueryRewriteResult。"""

    def __init__(self, result: QueryRewriteResult | None = None):
        self._result = result
        self.rewrite_called = False
        self.rewrite_args: dict[str, Any] = {}

    async def rewrite(self, query: str, context: str = "") -> QueryRewriteResult:
        self.rewrite_called = True
        self.rewrite_args = {"query": query, "context": context}
        if self._result is not None:
            return self._result
        return QueryRewriteResult(original=query)


class FailingQueryRewriter:
    """总是失败的 QueryRewriter。"""

    async def rewrite(self, query: str, context: str = "") -> QueryRewriteResult:
        raise RuntimeError("QueryRewriter failed")


class FakePlanner:
    """空计划 Planner — 不消耗 MultiResponseLLM 的有序响应。

    P1-9 起 engine 会在决策循环前调用 planner.build_initial_plan，
    真实 PlanManager 会消耗一次 LLM 响应，打乱本文件预设的
    ["retrieve", "generate"] 序列；空计划时 engine 跳过全部 plan 逻辑。
    """

    max_replans: int = 2

    async def build_initial_plan(self, query: str) -> Any:
        from app.agents.planner import PlanResult

        return PlanResult(steps=[], criteria=[])


# ======================================================================
# 辅助函数
# ======================================================================


def _filter_sse_events(events: list, event_type: str) -> list[SSEEvent]:
    """从事件列表中筛选指定类型的 SSEEvent。"""
    return [
        e for e in events
        if isinstance(e, SSEEvent) and e.event == event_type
    ]


def _make_engine_with_retrieve(
    candidates: list[dict[str, Any]] | None = None,
    query_rewriter=None,
) -> tuple:
    """构造先 retrieve 再 generate 的 engine。"""
    from app.rag.engine import AgenticRAGEngine

    llm = MultiResponseLLM(["retrieve", "generate"])
    retriever = FakeRetriever(candidates)
    reranker = FakeReranker()
    generator = FakeGenerator()
    engine = AgenticRAGEngine(
        llm=llm,
        mcp_client=FakeMCPClient(),
        retriever=retriever,
        reranker=reranker,
        generator=generator,
        cache=None,
        max_iterations=5,
        query_rewriter=query_rewriter,
        planner=FakePlanner(),
    )
    return engine, llm, retriever, reranker


# ======================================================================
# engine 注入测试
# ======================================================================


class TestEngineQueryRewriterInjection:
    """QueryRewriter 注入到 engine 测试。"""

    def test_engine_accepts_query_rewriter(self):
        """engine 接受 query_rewriter 参数。"""
        rewriter = FakeQueryRewriter()
        engine, _, _, _ = _make_engine(query_rewriter=rewriter)
        assert engine._query_rewriter is rewriter

    def test_engine_without_query_rewriter(self):
        """未传入 query_rewriter 时 engine 正常创建。"""
        with patch("app.rag.query_rewriter.get_query_rewriter", return_value=None):
            engine, _, _, _ = _make_engine(query_rewriter=None)
            assert engine._query_rewriter is None

    def test_engine_auto_inits_query_rewriter(self):
        """未传入时 engine 自动从工厂获取。"""
        from unittest.mock import MagicMock

        mock_rewriter = MagicMock()
        with patch(
            "app.rag.query_rewriter.get_query_rewriter", return_value=mock_rewriter
        ):
            engine, _, _, _ = _make_engine(query_rewriter=None)
            assert engine._query_rewriter is mock_rewriter


# ======================================================================
# SSE 事件测试
# ======================================================================


class TestQueryRewriteSSEEvent:
    """answer() 发出 QUERY_REWRITE SSE 事件测试。"""

    @pytest.mark.asyncio
    async def test_answer_emits_query_rewrite_event(self):
        """answer() 发出 QUERY_REWRITE SSE 事件。"""
        rewrite_result = QueryRewriteResult(
            original="测试查询",
            rewritten="优化后的查询",
            strategy=["rewrite"],
            latency_ms=50.0,
        )
        rewriter = FakeQueryRewriter(result=rewrite_result)

        engine, _, _, _ = _make_engine_with_retrieve(
            query_rewriter=rewriter,
        )

        events = []
        async for event in engine.answer("测试查询", "user-001", "session-001"):
            events.append(event)

        # 应该有 QUERY_REWRITE 事件
        rewrite_events = _filter_sse_events(events, SSEEventType.QUERY_REWRITE)
        assert len(rewrite_events) == 1

        # 验证事件数据
        event_data = rewrite_events[0].data
        assert event_data["original"] == "测试查询"
        assert event_data["rewritten"] == "优化后的查询"
        assert "rewrite" in event_data["strategy"]

    @pytest.mark.asyncio
    async def test_no_query_rewrite_event_when_rewriter_none(self):
        """QueryRewriter 为 None 时不发出 QUERY_REWRITE 事件。"""
        with patch("app.rag.query_rewriter.get_query_rewriter", return_value=None):
            engine, _, _, _ = _make_engine_with_retrieve()

            events = []
            async for event in engine.answer("测试查询", "user-001", "session-001"):
                events.append(event)

            rewrite_events = _filter_sse_events(events, SSEEventType.QUERY_REWRITE)
            assert len(rewrite_events) == 0


# ======================================================================
# 检索使用重写查询测试
# ======================================================================


class TestRetrievalUsesRewrittenQuery:
    """检索使用重写后的查询测试。"""

    @pytest.mark.asyncio
    async def test_retriever_uses_rewritten_query(self):
        """检索器使用重写后的查询而非原始查询。"""
        rewrite_result = QueryRewriteResult(
            original="原始查询",
            rewritten="重写后的查询文本",
            strategy=["rewrite"],
        )
        rewriter = FakeQueryRewriter(result=rewrite_result)

        engine, _, retriever, _ = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=rewriter,
        )

        async for _ in engine.answer("原始查询", "user-001", "session-001"):
            pass

        assert retriever.search_query == "重写后的查询文本"

    @pytest.mark.asyncio
    async def test_reranker_uses_original_query(self):
        """重排器使用原始查询而非重写查询。"""
        rewrite_result = QueryRewriteResult(
            original="原始查询",
            rewritten="重写后的查询文本",
            strategy=["rewrite"],
        )
        rewriter = FakeQueryRewriter(result=rewrite_result)

        engine, _, _, reranker = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档内容", "score": 0.8}],
            query_rewriter=rewriter,
        )

        async for _ in engine.answer("原始查询", "user-001", "session-001"):
            pass

        assert reranker.rerank_query == "原始查询"

    @pytest.mark.asyncio
    async def test_hyde_document_used_for_retrieval(self):
        """HyDE 文档用于检索，原始查询用于重排。"""
        hyde_doc = "这是一段假设性的文档内容，描述了相关信息的细节。"
        rewrite_result = QueryRewriteResult(
            original="年假政策",
            hyde_document=hyde_doc,
            strategy=["hyde"],
        )
        rewriter = FakeQueryRewriter(result=rewrite_result)

        engine, _, retriever, reranker = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=rewriter,
        )

        async for _ in engine.answer("年假政策", "user-001", "session-001"):
            pass

        assert retriever.search_query == hyde_doc
        assert reranker.rerank_query == "年假政策"


# ======================================================================
# 降级测试
# ======================================================================


class TestQueryRewriterDegradation:
    """QueryRewriter 失败时降级测试。"""

    @pytest.mark.asyncio
    async def test_failing_rewriter_does_not_break_answer(self):
        """QueryRewriter 失败不影响 answer 正常流程。"""
        rewriter = FailingQueryRewriter()

        engine, _, retriever, _ = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=rewriter,
        )

        events = []
        async for event in engine.answer("测试查询", "user-001", "session-001"):
            events.append(event)

        # 不应该有 QUERY_REWRITE 事件
        rewrite_events = _filter_sse_events(events, SSEEventType.QUERY_REWRITE)
        assert len(rewrite_events) == 0

        # 检索使用原始查询（降级）
        assert retriever.search_query == "测试查询"

    @pytest.mark.asyncio
    async def test_rewriter_with_no_rewrite_uses_original(self):
        """QueryRewriter 返回无重写时使用原始查询。"""
        rewrite_result = QueryRewriteResult(
            original="测试查询",
            rewritten="",
            strategy=[],
        )
        rewriter = FakeQueryRewriter(result=rewrite_result)

        engine, _, retriever, _ = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=rewriter,
        )

        async for _ in engine.answer("测试查询", "user-001", "session-001"):
            pass

        assert retriever.search_query == "测试查询"


# ======================================================================
# 幂等性测试
# ======================================================================


class TestEngineQueryRewriteIdempotency:
    """engine 查询重写幂等性测试。"""

    @pytest.mark.asyncio
    async def test_same_query_produces_same_rewrite_event(self):
        """相同查询产生一致的 QUERY_REWRITE 事件。"""
        rewrite_result = QueryRewriteResult(
            original="测试查询",
            rewritten="优化查询",
            strategy=["rewrite"],
            latency_ms=50.0,
        )

        # 第一次调用
        engine1, _, _, _ = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=FakeQueryRewriter(result=rewrite_result),
        )
        events1 = []
        async for event in engine1.answer("测试查询", "user-001", "session-001"):
            events1.append(event)

        # 第二次调用
        engine2, _, _, _ = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=FakeQueryRewriter(result=rewrite_result),
        )
        events2 = []
        async for event in engine2.answer("测试查询", "user-001", "session-001"):
            events2.append(event)

        # QUERY_REWRITE 事件数据一致
        rw1 = _filter_sse_events(events1, SSEEventType.QUERY_REWRITE)
        rw2 = _filter_sse_events(events2, SSEEventType.QUERY_REWRITE)
        assert len(rw1) == 1
        assert len(rw2) == 1
        assert rw1[0].data["rewritten"] == rw2[0].data["rewritten"]
        assert rw1[0].data["strategy"] == rw2[0].data["strategy"]


# ======================================================================
# 日志验证测试
# ======================================================================


class TestEngineQueryRewriteLogging:
    """engine 查询重写日志验证测试。"""

    @pytest.mark.asyncio
    async def test_engine_logs_query_rewritten(self):
        """engine 记录 query_rewritten 日志。"""
        rewrite_result = QueryRewriteResult(
            original="原始查询",
            rewritten="重写查询",
            strategy=["rewrite"],
            latency_ms=42.0,
        )
        rewriter = FakeQueryRewriter(result=rewrite_result)

        engine, _, _, _ = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=rewriter,
        )

        with patch("app.rag.engine.log") as mock_log:
            async for _ in engine.answer("原始查询", "user-001", "session-001"):
                pass

            log_calls = [str(c) for c in mock_log.info.call_args_list]
            assert any("engine.query_rewritten" in c for c in log_calls)

    @pytest.mark.asyncio
    async def test_engine_logs_query_rewrite_failed(self):
        """engine 记录 query_rewrite_failed 日志。"""
        rewriter = FailingQueryRewriter()

        engine, _, _, _ = _make_engine_with_retrieve(
            candidates=[{"id": "1", "content": "文档", "score": 0.8}],
            query_rewriter=rewriter,
        )

        with patch("app.rag.engine.log") as mock_log:
            async for _ in engine.answer("测试查询", "user-001", "session-001"):
                pass

            log_calls = [str(c) for c in mock_log.warning.call_args_list]
            assert any("engine.query_rewrite_failed" in c for c in log_calls)
