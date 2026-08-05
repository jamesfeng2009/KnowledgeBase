"""Token 优化测试 — P0-Opt1 Prompt Caching + P0-Opt2 Live-Zone 增量传递。

覆盖两个 P0 优化点：
- P0-Opt1: Anthropic Prompt Caching — cache_aligner 检测 + provider cache_control 标记
- P0-Opt2: Live-Zone 增量上下文 — system prompt 稳定化 + 增量结果追加 + 向后兼容
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.cache_aligner import check_cache_alignment
from app.rag.engine import AgentState, AgenticRAGEngine, _THINK_SYSTEM_STABLE


# ======================================================================
# Mock 实现（复用 test_rag_engine.py 的 Mock，扩展消息记录能力）
# ======================================================================


class MessageRecordingLLM:
    """Mock LLM — 记录每次 chat 调用收到的 messages，用于验证增量传递。"""

    def __init__(self, response: str = "generate") -> None:
        self.response = response
        self.call_history: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        self.call_history.append(list(messages))
        yield self.response


class FakeRetriever:
    """Mock HybridRetriever — 返回预设候选文档。"""

    def __init__(self, candidates: list[dict[str, Any]] | None = None) -> None:
        self.candidates = candidates or []

    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        return self.candidates


class FakeReranker:
    """Mock RerankerBase — 记录调用与接收到的文档。"""

    def __init__(self) -> None:
        self.called: bool = False
        self.received_docs: list[dict[str, Any]] = []

    async def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        self.called = True
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
# 辅助函数
# ======================================================================


def _make_engine(
    llm_response: str = "generate",
    candidates: list[dict[str, Any]] | None = None,
    max_iterations: int = 5,
) -> tuple[AgenticRAGEngine, MessageRecordingLLM, FakeRetriever, FakeReranker]:
    """构造带消息记录能力的 Mock 引擎。"""
    llm = MessageRecordingLLM(llm_response)
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
        max_iterations=max_iterations,
    )
    # 本测试组聚焦 think 的 system prompt 稳定性与 messages 增量，
    # 不验证 Planner 行为；禁用自动创建的 PlanManager 避免其 LLM 调用
    # （_PLAN_PROMPT「任务规划专家」）混入 call_history 污染断言。
    engine._planner = None
    return engine, llm, retriever, reranker


def _make_state(**overrides: Any) -> AgentState:
    """构造测试用 AgentState。"""
    state: AgentState = {
        "query": "报销流程怎么走",
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


# ======================================================================
# P0-Opt1: Cache Aligner 测试
# ======================================================================


class TestCacheAligner:
    """P0-Opt1: CacheAligner 易变内容检测测试。"""

    def test_detects_uuid(self) -> None:
        """UUID 会破坏 KV Cache 前缀稳定性。"""
        text = "Session ID: 550e8400-e29b-41d4-a716-446655440000"
        warnings = check_cache_alignment(text)
        assert any("UUID" in w for w in warnings)

    def test_detects_iso8601_timestamp(self) -> None:
        """ISO8601 时间戳会破坏缓存前缀。"""
        text = "Current time: 2026-07-13T14:30:00"
        warnings = check_cache_alignment(text)
        assert any("ISO8601" in w for w in warnings)

    def test_detects_jwt_token(self) -> None:
        """JWT token 会破坏缓存前缀。"""
        text = "Auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
        warnings = check_cache_alignment(text)
        assert any("JWT" in w for w in warnings)

    def test_detects_hex_hash(self) -> None:
        """40+ 位十六进制哈希会破坏缓存前缀。"""
        text = "Commit: a1b2c3d4e5f6789012345abcdef1234567890abcd"
        warnings = check_cache_alignment(text)
        assert any("hex hash" in w for w in warnings)

    def test_clean_text_no_warnings(self) -> None:
        """稳定的 system prompt 不应产生警告。"""
        text = _THINK_SYSTEM_STABLE  # 稳定 prompt，无易变内容
        warnings = check_cache_alignment(text)
        assert warnings == []

    def test_multiple_volatile_patterns(self) -> None:
        """多种易变内容同时存在时应全部检测到。"""
        text = (
            "Session: 550e8400-e29b-41d4-a716-446655440000\n"
            "Time: 2026-07-13T14:30:00\n"
            "Token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
        )
        warnings = check_cache_alignment(text)
        assert len(warnings) >= 3

    def test_empty_text_no_warnings(self) -> None:
        """空文本不应产生警告。"""
        assert check_cache_alignment("") == []


# ======================================================================
# P0-Opt1: Anthropic Provider Prompt Caching 测试
# ======================================================================


class TestAnthropicPromptCaching:
    """P0-Opt1: Anthropic Provider 的 cache_control 标记测试。"""

    def test_build_api_kwargs_adds_cache_control(self) -> None:
        """_build_api_kwargs 应为 system prompt 添加 cache_control。"""
        from app.llm.anthropic_provider import AnthropicProvider

        # Mock 构造函数避免需要真实 API key
        provider = object.__new__(AnthropicProvider)
        provider.default_model = "claude-sonnet-4-6-20260217"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "你是一个助手。"},
            {"role": "user", "content": "你好"},
        ]

        api_kwargs = provider._build_api_kwargs(messages, None, {})

        # system 应为 content block 列表，带 cache_control
        assert "system" in api_kwargs
        assert isinstance(api_kwargs["system"], list)
        assert len(api_kwargs["system"]) == 1
        block = api_kwargs["system"][0]
        assert block["type"] == "text"
        assert block["text"] == "你是一个助手。"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_build_api_kwargs_no_system_no_cache(self) -> None:
        """没有 system 消息时不应添加 cache_control。"""
        from app.llm.anthropic_provider import AnthropicProvider

        provider = object.__new__(AnthropicProvider)
        provider.default_model = "claude-sonnet-4-6-20260217"

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "你好"},
        ]

        api_kwargs = provider._build_api_kwargs(messages, None, {})

        # 没有 system 时，api_kwargs 中不应有 system 键
        assert "system" not in api_kwargs

    def test_build_api_kwargs_preserves_messages(self) -> None:
        """cache_control 不应影响 non-system messages 的结构。"""
        from app.llm.anthropic_provider import AnthropicProvider

        provider = object.__new__(AnthropicProvider)
        provider.default_model = "claude-sonnet-4-6-20260217"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "query"},
            {"role": "assistant", "content": "response"},
        ]

        api_kwargs = provider._build_api_kwargs(messages, None, {})

        # non-system messages 应保持原样
        assert len(api_kwargs["messages"]) == 2
        assert api_kwargs["messages"][0]["role"] == "user"
        assert api_kwargs["messages"][1]["role"] == "assistant"


# ======================================================================
# P0-Opt2: Live-Zone 增量上下文传递测试
# ======================================================================


class TestStableSystemPrompt:
    """P0-Opt2: 稳定 system prompt 测试。"""

    def test_think_system_stable_is_constant(self) -> None:
        """_THINK_SYSTEM_STABLE 应是不含动态内容的常量。"""
        # 不含动态变量（迭代计数/文档数/工具结果数）
        assert "当前迭代" not in _THINK_SYSTEM_STABLE
        assert "已检索文档数" not in _THINK_SYSTEM_STABLE
        assert "已调用工具结果数" not in _THINK_SYSTEM_STABLE
        assert "工具结果数" not in _THINK_SYSTEM_STABLE
        # 应包含三个决策关键词
        assert "retrieve" in _THINK_SYSTEM_STABLE
        assert "tool_call" in _THINK_SYSTEM_STABLE
        assert "generate" in _THINK_SYSTEM_STABLE

    def test_think_system_stable_passes_cache_aligner(self) -> None:
        """稳定 prompt 不应触发 CacheAligner 警告。"""
        warnings = check_cache_alignment(_THINK_SYSTEM_STABLE)
        assert warnings == []


class TestIncrementalContext:
    """P0-Opt2: 增量上下文传递测试。"""

    @pytest.mark.asyncio
    async def test_think_uses_state_messages_when_available(self) -> None:
        """_think 应使用 state["messages"] 作为基础，而非重建。"""
        engine, llm, _, _ = _make_engine(llm_response="generate")

        # 预设 state["messages"] 为稳定前缀
        state = _make_state(
            iteration=1,
            messages=[
                {"role": "system", "content": _THINK_SYSTEM_STABLE},
                {"role": "user", "content": "测试问题"},
            ],
        )

        await engine._think(state)

        # LLM 应收到 state["messages"] 的内容
        assert len(llm.call_history) == 1
        sent_messages = llm.call_history[0]

        # 前两条应为预设的稳定前缀
        assert sent_messages[0]["role"] == "system"
        assert sent_messages[0]["content"] == _THINK_SYSTEM_STABLE
        assert sent_messages[1]["role"] == "user"
        assert sent_messages[1]["content"] == "测试问题"

    @pytest.mark.asyncio
    async def test_think_falls_back_when_messages_empty(self) -> None:
        """state["messages"] 为空时应从稳定 prompt + query 构建（向后兼容）。"""
        engine, llm, _, _ = _make_engine(llm_response="generate")
        state = _make_state(iteration=1, messages=[])

        await engine._think(state)

        assert len(llm.call_history) == 1
        sent_messages = llm.call_history[0]

        # 应从稳定 prompt 构建
        assert sent_messages[0]["role"] == "system"
        assert sent_messages[0]["content"] == _THINK_SYSTEM_STABLE
        assert sent_messages[1]["role"] == "user"
        assert sent_messages[1]["content"] == "报销流程怎么走"

    @pytest.mark.asyncio
    async def test_think_appends_dynamic_context(self) -> None:
        """_think 应在末尾追加动态上下文（live zone）。"""
        engine, llm, _, _ = _make_engine(llm_response="generate")
        state = _make_state(
            iteration=2,
            messages=[
                {"role": "system", "content": _THINK_SYSTEM_STABLE},
                {"role": "user", "content": "测试问题"},
            ],
            retrieved_docs=[{"content": "doc1"}, {"content": "doc2"}],
            tool_results=[{"result": "tool1"}],
        )

        await engine._think(state)

        sent_messages = llm.call_history[0]
        # 最后一条应为动态上下文
        last_msg = sent_messages[-1]
        assert last_msg["role"] == "user"
        assert "迭代 2/5" in last_msg["content"]
        assert "文档 2 篇" in last_msg["content"]
        assert "工具结果 1 条" in last_msg["content"]

    @pytest.mark.asyncio
    async def test_think_does_not_modify_state_messages(self) -> None:
        """_think 不应修改 state["messages"]（只读取，追加到副本）。"""
        engine, llm, _, _ = _make_engine(llm_response="generate")
        original_messages = [
            {"role": "system", "content": _THINK_SYSTEM_STABLE},
            {"role": "user", "content": "测试问题"},
        ]
        state = _make_state(iteration=1, messages=list(original_messages))

        await engine._think(state)

        # state["messages"] 不应被修改
        assert len(state["messages"]) == 2
        assert state["messages"] == original_messages

    @pytest.mark.asyncio
    async def test_system_prompt_same_across_iterations(self) -> None:
        """多轮 think 调用中 system prompt 应保持字节稳定。"""
        engine, llm, _, _ = _make_engine(llm_response="retrieve")
        state = _make_state(iteration=0, messages=[
            {"role": "system", "content": _THINK_SYSTEM_STABLE},
            {"role": "user", "content": "测试问题"},
        ])

        # 模拟 3 轮 think
        for i in range(1, 4):
            state["iteration"] = i
            await engine._think(state)

        # 3 次调用的 system prompt 应完全相同
        assert len(llm.call_history) == 3
        system_1 = llm.call_history[0][0]["content"]
        system_2 = llm.call_history[1][0]["content"]
        system_3 = llm.call_history[2][0]["content"]
        assert system_1 == system_2 == system_3 == _THINK_SYSTEM_STABLE

    @pytest.mark.asyncio
    async def test_query_not_repeated_in_messages(self) -> None:
        """query 应只在稳定前缀中出现一次，不在动态上下文中重复。"""
        engine, llm, _, _ = _make_engine(llm_response="generate")
        state = _make_state(iteration=1, messages=[
            {"role": "system", "content": _THINK_SYSTEM_STABLE},
            {"role": "user", "content": "报销流程怎么走"},
        ])

        await engine._think(state)

        sent_messages = llm.call_history[0]
        # query 只在第二条消息中出现
        query_count = sum(
            1 for m in sent_messages if m["content"] == "报销流程怎么走"
        )
        assert query_count == 1


class TestRunDecisionLoopIncremental:
    """P0-Opt2: _run_decision_loop 增量行为测试。"""

    @pytest.mark.asyncio
    async def test_loop_initializes_stable_prefix(self) -> None:
        """_run_decision_loop 应初始化 state["messages"] 为稳定前缀。"""
        engine, llm, _, _ = _make_engine(llm_response="generate")
        state = _make_state(messages=[])

        await engine._run_decision_loop(state)

        # messages 应被初始化为 [system, user]
        assert len(state["messages"]) >= 2
        assert state["messages"][0]["role"] == "system"
        assert state["messages"][0]["content"] == _THINK_SYSTEM_STABLE
        assert state["messages"][1]["role"] == "user"
        assert state["messages"][1]["content"] == "报销流程怎么走"

    @pytest.mark.asyncio
    async def test_loop_appends_after_retrieve(self) -> None:
        """retrieve 后应追加增量结果摘要到 messages。"""
        candidates = [{"content": "doc1"}, {"content": "doc2"}, {"content": "doc3"}]
        engine, llm, _, _ = _make_engine(
            llm_response="retrieve",
            candidates=candidates,
            max_iterations=2,
        )
        state = _make_state()

        # 第一轮返回 retrieve，第二轮返回 generate
        llm.response = "retrieve"
        # 用 side_effect 模拟多轮
        responses = ["retrieve", "generate"]

        original_chat = llm.chat

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            llm.call_history.append(list(messages))
            yield responses[len(llm.call_history) - 1]

        llm.chat = mock_chat

        await engine._run_decision_loop(state)

        # messages 应包含稳定前缀 + retrieve 后的增量摘要
        assert len(state["messages"]) >= 3
        # 第三条应为 retrieve 结果摘要
        retrieve_msg = state["messages"][2]
        assert "已检索到" in retrieve_msg["content"]
        assert "3 篇文档" in retrieve_msg["content"]

    @pytest.mark.asyncio
    async def test_loop_system_prompt_stable_across_iterations(self) -> None:
        """多轮迭代中 system prompt 保持字节稳定。"""
        candidates = [{"content": "doc1"}]
        engine, llm, _, _ = _make_engine(
            candidates=candidates, max_iterations=3
        )
        state = _make_state()

        responses = ["retrieve", "generate"]
        call_idx = 0

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            llm.call_history.append(list(messages))
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        await engine._run_decision_loop(state)

        # 所有 think 调用的 system prompt 应相同
        for sent_messages in llm.call_history:
            if sent_messages[0]["role"] == "system":
                assert sent_messages[0]["content"] == _THINK_SYSTEM_STABLE

    @pytest.mark.asyncio
    async def test_loop_query_in_prefix_only(self) -> None:
        """query 只在稳定前缀中出现，不在增量消息中重复。"""
        engine, llm, _, _ = _make_engine(max_iterations=2)
        state = _make_state()

        responses = ["generate"]
        call_idx = 0

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            llm.call_history.append(list(messages))
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        await engine._run_decision_loop(state)

        # 在所有 think 调用中，query 只出现一次（在前缀中）
        for sent_messages in llm.call_history:
            query_count = sum(
                1 for m in sent_messages if m["content"] == "报销流程怎么走"
            )
            assert query_count <= 1, "query 在单次调用中被重复传递"


# ======================================================================
# 回归测试 — 确保原有功能不受影响
# ======================================================================


class TestBackwardCompatibility:
    """回归测试 — 确保 P0 优化不破坏原有功能。"""

    @pytest.mark.asyncio
    async def test_existing_think_test_still_passes(self) -> None:
        """原有 _think 测试应仍然通过（向后兼容）。"""
        from tests.test_rag_engine import FakeLLM as OriginalFakeLLM

        engine = AgenticRAGEngine(
            llm=OriginalFakeLLM("retrieve"),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )
        state = _make_state(iteration=1)

        decision = await engine._think(state)
        assert decision == "retrieve"

    @pytest.mark.asyncio
    async def test_existing_think_error_fallback(self) -> None:
        """LLM 异常时 _think 应降级返回 generate。"""
        class ErrorLLM:
            async def chat(self, *args, **kwargs):
                raise RuntimeError("LLM unavailable")
                yield  # noqa: E701

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

    @pytest.mark.asyncio
    async def test_answer_yields_tokens(self) -> None:
        """answer 主入口应正常流式输出 token。"""
        from tests.test_rag_engine import FakeLLM as OriginalFakeLLM

        engine = AgenticRAGEngine(
            llm=OriginalFakeLLM("generate"),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )

        tokens = []
        async for chunk in engine.answer("test query", "user-1", "session-1"):
            if isinstance(chunk, str):
                tokens.append(chunk)

        assert "".join(tokens) == "这是答案"

    @pytest.mark.asyncio
    async def test_parse_decision_unchanged(self) -> None:
        """_parse_decision 行为不变。"""
        assert AgenticRAGEngine._parse_decision("retrieve") == "retrieve"
        assert AgenticRAGEngine._parse_decision("tool_call") == "tool_call"
        assert AgenticRAGEngine._parse_decision("generate") == "generate"
        assert AgenticRAGEngine._parse_decision("random") == "generate"

    def test_agent_state_still_has_messages_field(self) -> None:
        """AgentState 仍包含 messages 字段。"""
        state = _make_state()
        assert "messages" in state
        assert isinstance(state["messages"], list)
