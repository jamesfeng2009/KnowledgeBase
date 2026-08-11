"""RAG 引擎测试 — AgenticRAGEngine 的决策循环、检索与反思。

使用 Mock 实现 LLM / 检索器 / 重排器 / 生成器，隔离外部依赖，
聚焦验证引擎的流程编排逻辑（think → retrieve/tool_call → generate → reflect）。

核心验证点：
- AgentState 初始化字段完整；
- _parse_decision 正确解析三种路由信号；
- _think 委托 LLM 并返回决策；
- 权限过滤在重排之前执行（核心安全约束）；
- _reflect 正常执行不抛异常。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.rag.engine import AgentState, AgenticRAGEngine


# ======================================================================
# Mock 实现
# ======================================================================


class FakeLLM:
    """Mock LLM Provider — 按预设文本响应 chat 调用。"""

    def __init__(self, response: str = "generate") -> None:
        self.response = response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        yield self.response


class FakeRetriever:
    """Mock HybridRetriever — 返回预设候选文档，记录接收到的查询。"""

    def __init__(self, candidates: list[dict[str, Any]] | None = None) -> None:
        self.candidates = candidates or []
        self.search_query: str = ""

    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.search_query = query
        return self.candidates


class FakeReranker:
    """Mock RerankerBase — 记录调用与接收到的查询和文档。"""

    def __init__(self) -> None:
        self.called: bool = False
        self.received_docs: list[dict[str, Any]] = []
        self.rerank_query: str = ""

    async def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        self.called = True
        self.rerank_query = query
        self.received_docs = list(documents)
        return [
            {"index": i, "score": 0.9 - i * 0.1, "content": d.get("content", "")}
            for i, d in enumerate(documents)
        ]


class FakeGenerator:
    """Mock Generator — 逐 token yield 预设文本。"""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["这是", "答案"]

    async def generate(
        self,
        query: str,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str = "",
    ) -> AsyncIterator[str]:
        for token in self.tokens:
            yield token


class FakeMCPClient:
    """Mock MCPClient — 无工具可用。"""

    async def get_tools_for_llm(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        return "{}"


# ======================================================================
# 测试
# ======================================================================


def _make_engine(
    llm_response: str = "generate",
    candidates: list[dict[str, Any]] | None = None,
    permission_filter=None,
    max_iterations: int = 5,
    query_rewriter=None,
) -> tuple[AgenticRAGEngine, FakeLLM, FakeRetriever, FakeReranker]:
    """构造带 Mock 组件的 RAG 引擎。"""
    llm = FakeLLM(llm_response)
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
        permission_filter=permission_filter,
        max_iterations=max_iterations,
        query_rewriter=query_rewriter,
    )
    return engine, llm, retriever, reranker


def _make_state(**overrides: Any) -> AgentState:
    """构造测试用 AgentState。"""
    state: AgentState = {
        "query": "报销流程怎么走",
        "rewritten_query": None,
        "user_id": "user-001",
        "session_id": "session-001",
        "messages": [],
        "retrieved_docs": [],
        "tool_results": [],
        "answer": "",
        "iteration": 0,
        "max_iterations": 5,
        "kb_ids": None,
        "memory_context": "",
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


# ----------------------------------------------------------------------


class TestAgentStateInitialization:
    """AgentState 初始化测试。"""

    def test_agent_state_initialization(self) -> None:
        """AgentState 应包含所有必需字段且初始值正确。"""
        state = _make_state()

        assert state["query"] == "报销流程怎么走"
        assert state["user_id"] == "user-001"
        assert state["session_id"] == "session-001"
        assert state["messages"] == []
        assert state["retrieved_docs"] == []
        assert state["tool_results"] == []
        assert state["answer"] == ""
        assert state["iteration"] == 0
        assert state["max_iterations"] == 5

    def test_engine_stores_components(self) -> None:
        """引擎应正确存储注入的组件。"""
        engine, llm, retriever, reranker = _make_engine()

        assert engine.llm is llm
        assert engine.retriever is retriever
        assert engine.reranker is reranker
        assert engine.max_iterations == 5


class TestParseDecision:
    """_parse_decision 路由信号解析测试。"""

    def test_parse_decision_retrieve(self) -> None:
        """包含 'retrieve' 的文本应解析为 retrieve。"""
        assert AgenticRAGEngine._parse_decision("please retrieve knowledge") == "retrieve"

    def test_parse_decision_tool_call(self) -> None:
        """包含 'tool' 的文本应解析为 tool_call。"""
        assert AgenticRAGEngine._parse_decision("use tool to query") == "tool_call"

    def test_parse_decision_generate(self) -> None:
        """不匹配 retrieve/tool 的文本应解析为 generate。"""
        assert AgenticRAGEngine._parse_decision("generate the answer") == "generate"
        assert AgenticRAGEngine._parse_decision("random text") == "generate"


class TestThink:
    """_think 决策测试。"""

    @pytest.mark.asyncio
    async def test_think_returns_decision(self) -> None:
        """_think 应委托 LLM 并返回解析后的决策信号。"""
        engine, _, _, _ = _make_engine(llm_response="retrieve")
        state = _make_state(iteration=1)

        decision = await engine._think(state)

        assert decision == "retrieve"

    @pytest.mark.asyncio
    async def test_think_fallback_on_error(self) -> None:
        """LLM 异常时 _think 应降级返回 generate。"""

        class ErrorLLM:
            async def chat(self, *args, **kwargs):
                raise RuntimeError("LLM unavailable")
                yield  # noqa: E701 — 使其为 async generator

        engine = AgenticRAGEngine(
            llm=ErrorLLM(),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )
        state = _make_state(iteration=1)

        decision = await engine._think(state)

        assert decision == "generate"


class TestRetrieve:
    """_retrieve 检索流程测试 — 重点验证权限过滤在重排之前。"""

    @pytest.mark.asyncio
    async def test_retrieve_permission_filter_before_rerank(self) -> None:
        """权限过滤必须在重排之前执行。

        验证：
        1. permission_filter 先于 reranker.rerank 被调用；
        2. reranker 接收到的是过滤后的文档（不含越权文档）。
        """
        candidates = [
            {"chunk_id": "1", "content": "公开文档", "classification": "public"},
            {"chunk_id": "2", "content": "机密文档", "classification": "secret"},
            {"chunk_id": "3", "content": "内部文档", "classification": "internal"},
        ]

        call_order: list[str] = []

        async def permission_filter(docs: list[dict]) -> list[dict]:
            call_order.append("permission_filter")
            # 过滤掉 secret 文档
            return [d for d in docs if d.get("classification") != "secret"]

        class OrderTrackingReranker:
            def __init__(self) -> None:
                self.received_docs: list[dict] = []

            async def rerank(self, query, documents, top_k=5):
                call_order.append("rerank")
                self.received_docs = list(documents)
                return [
                    {"index": i, "score": 0.9, "content": d.get("content", "")}
                    for i, d in enumerate(documents)
                ]

        reranker = OrderTrackingReranker()
        engine = AgenticRAGEngine(
            llm=FakeLLM(),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(candidates),
            reranker=reranker,
            generator=FakeGenerator(),
            permission_filter=permission_filter,
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=None)

        # 权限过滤在重排之前
        assert call_order[0] == "permission_filter"
        assert call_order[1] == "rerank"
        # 重排器收到的是过滤后的文档（不含 secret）
        assert len(reranker.received_docs) == 2
        assert all(
            d.get("classification") != "secret" for d in reranker.received_docs
        )

    @pytest.mark.asyncio
    async def test_retrieve_without_permission_filter(self) -> None:
        """未注入 permission_filter 时，所有候选直接进入重排。"""
        candidates = [
            {"chunk_id": "1", "content": "文档 A"},
            {"chunk_id": "2", "content": "文档 B"},
        ]
        engine, _, _, reranker = _make_engine(
            candidates=candidates, permission_filter=None
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=None)

        assert reranker.called
        assert len(reranker.received_docs) == 2

    @pytest.mark.asyncio
    async def test_retrieve_permission_error_returns_empty(self) -> None:
        """权限过滤异常时应保守返回空列表，避免泄露越权文档。"""

        async def error_filter(docs: list[dict]) -> list[dict]:
            raise RuntimeError("permission service down")

        engine, _, _, reranker = _make_engine(
            candidates=[{"chunk_id": "1", "content": "doc"}],
            permission_filter=error_filter,
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=None)

        # 权限过滤出错 -> 重排器未被调用 -> retrieved_docs 为空
        assert not reranker.called
        assert state["retrieved_docs"] == []


class TestReflect:
    """_reflect 反思测试。"""

    @pytest.mark.asyncio
    async def test_reflect_quality_check(self) -> None:
        """_reflect 应正常执行并记录反思结论，不抛异常。"""
        engine, _, _, _ = _make_engine(llm_response="satisfied")
        state = _make_state(answer="这是一个完整的回答，包含引用来源。")

        # 不应抛出异常
        await engine._reflect(state)

        # answer 未被修改
        assert "完整的回答" in state["answer"]


class TestAnswerStream:
    """answer 主入口流式输出测试。"""

    @pytest.mark.asyncio
    async def test_answer_yields_tokens(self) -> None:
        """answer 应流式 yield 生成器的 token。"""
        engine, _, _, _ = _make_engine(llm_response="generate")

        tokens = []
        async for chunk in engine.answer("test query", "user-1", "session-1"):
            if isinstance(chunk, str):
                tokens.append(chunk)

        assert "".join(tokens) == "这是答案"
