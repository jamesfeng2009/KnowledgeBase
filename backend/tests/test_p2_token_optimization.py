"""P2 Token 优化测试 — Think 上下文上限保护 + 压缩摘要。

覆盖 P2-Opt6 优化点：
- ContextBudgetManager — token 估算 / 预算检查 / 三段式压缩
- engine.py _run_decision_loop 集成 — 每轮 think 前检查并压缩
- 回归测试 — 确保短对话不触发压缩，已有功能不受影响
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.context_budget import ContextBudgetManager
from app.rag.engine import AgenticRAGEngine, _THINK_SYSTEM_STABLE


# ======================================================================
# Mock 实现（复用 test_p1_token_optimization.py 的 Mock）
# ======================================================================


class MessageRecordingLLM:
    """Mock LLM — 记录每次 chat 调用收到的 messages。"""

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
    def __init__(self, candidates: list[dict[str, Any]] | None = None) -> None:
        self.candidates = candidates or []

    async def search(self, query, kb_ids=None, top_k=20):
        return self.candidates


class FakeReranker:
    async def rerank(self, query, documents, top_k=5):
        return [
            {"index": i, "score": 0.9 - i * 0.1, "content": d.get("content", "")}
            for i, d in enumerate(documents)
        ]


class FakeGenerator:
    def __init__(self, tokens=None):
        self.tokens = tokens or ["这是", "答案"]

    async def generate(
        self,
        query,
        retrieved_docs,
        tool_results,
        memory_context="",
        constraint_context=None,
    ):
        for token in self.tokens:
            yield token


class FakeMCPClient:
    async def get_tools_for_llm(self):
        return []

    async def call_tool(self, tool_name, arguments):
        return "{}"


def _make_engine(
    llm_response: str = "generate",
    max_iterations: int = 5,
) -> tuple[AgenticRAGEngine, MessageRecordingLLM]:
    llm = MessageRecordingLLM(llm_response)
    engine = AgenticRAGEngine(
        llm=llm,
        mcp_client=FakeMCPClient(),
        retriever=FakeRetriever(),
        reranker=FakeReranker(),
        generator=FakeGenerator(),
        cache=None,
        max_iterations=max_iterations,
    )
    # 禁用自动创建的 PlanManager：本测试组聚焦 budget 压缩触发条件，
    # Planner 的 LLM 调用会消耗 mock responses 序列污染断言。
    engine._planner = None
    return engine, llm


def _make_state(**overrides: Any) -> dict:
    state: dict[str, Any] = {
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
    state.update(overrides)
    return state


def _make_long_messages(count: int = 10, content_size: int = 500) -> list[dict[str, Any]]:
    """生成足够长的消息列表以触发预算压缩。

    每条消息 content_size 字符，总 token ≈ count * content_size / 3.5
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _THINK_SYSTEM_STABLE},
        {"role": "user", "content": "用户原始问题" * 10},
    ]
    for i in range(count):
        messages.append({
            "role": "user",
            "content": f"[系统] 工具结果：这是第{i+1}轮工具调用的结果，" + "x" * content_size,
        })
    return messages


# ======================================================================
# ContextBudgetManager — token 估算测试
# ======================================================================


class TestEstimateTokens:
    """ContextBudgetManager.estimate_tokens 测试。"""

    def test_empty_messages(self) -> None:
        """空消息列表 token 数为 0。"""
        assert ContextBudgetManager.estimate_tokens([]) == 0

    def test_single_short_message(self) -> None:
        """单条短消息的 token 估算。"""
        messages = [{"role": "user", "content": "hello world"}]  # 11 chars（非 CJK）
        tokens = ContextBudgetManager.estimate_tokens(messages)
        assert tokens == int(11 / 4.0)  # 非 CJK 4.0 字符/token ≈ 2

    def test_multiple_messages(self) -> None:
        """多条消息的 token 估算为各消息字符数之和除以系数。"""
        messages = [
            {"role": "system", "content": "a" * 100},
            {"role": "user", "content": "b" * 200},
        ]
        tokens = ContextBudgetManager.estimate_tokens(messages)
        assert tokens == int(300 / 4.0)  # 非 CJK 4.0 字符/token = 75

    def test_missing_content_field(self) -> None:
        """缺少 content 字段的消息按 0 字符处理。"""
        messages = [{"role": "user"}, {"role": "system", "content": "hello"}]
        tokens = ContextBudgetManager.estimate_tokens(messages)
        assert tokens == int(5 / 4.0)  # 非 CJK 4.0 字符/token = 1


# ======================================================================
# ContextBudgetManager — should_compress 测试
# ======================================================================


class TestShouldCompress:
    """ContextBudgetManager.should_compress 测试。"""

    def test_below_threshold_no_compress(self) -> None:
        """token 数低于阈值时不压缩。"""
        manager = ContextBudgetManager(max_tokens=2000)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "query"},
            {"role": "user", "content": "short message"},
            {"role": "user", "content": "another short message"},
        ]
        assert not manager.should_compress(messages)

    def test_above_threshold_should_compress(self) -> None:
        """token 数超过阈值时需要压缩。"""
        manager = ContextBudgetManager(max_tokens=100)  # 很低的阈值
        messages = _make_long_messages(count=5, content_size=500)
        assert manager.should_compress(messages)

    def test_too_few_messages_no_compress(self) -> None:
        """消息数 ≤ 2 + keep_recent 时不压缩（没有中间消息可压缩）。"""
        manager = ContextBudgetManager(max_tokens=10, keep_recent=2)
        # 只有 4 条消息 = 2(head) + 2(tail)，无中间消息
        messages = [
            {"role": "system", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
        ]
        assert not manager.should_compress(messages)

    def test_custom_keep_recent(self) -> None:
        """自定义 keep_recent 参数生效。"""
        manager = ContextBudgetManager(max_tokens=10, keep_recent=3)
        # 5 条消息 = 2(head) + 3(tail)，无中间消息
        messages = [
            {"role": "system", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
        ]
        assert not manager.should_compress(messages)
        # 6 条消息 = 2(head) + 3(tail) + 1(middle)，有中间消息
        messages.append({"role": "user", "content": "x" * 100})
        assert manager.should_compress(messages)


# ======================================================================
# ContextBudgetManager — compress 测试
# ======================================================================


class TestCompress:
    """ContextBudgetManager.compress 测试。"""

    def test_preserves_head_system_and_query(self) -> None:
        """压缩后保留前 2 条（system + query）。"""
        manager = ContextBudgetManager(max_tokens=100, keep_recent=2)
        messages = _make_long_messages(count=5, content_size=500)
        result = manager.compress(messages)

        assert result[0] == messages[0]  # system
        assert result[1] == messages[1]  # query

    def test_preserves_tail_recent_messages(self) -> None:
        """压缩后保留最后 keep_recent 条消息。"""
        # P0-2: max_tokens=300 使 ratio 落在 HISTORY_SUMMARY（三段式摘要）
        manager = ContextBudgetManager(max_tokens=300, keep_recent=2)
        messages = _make_long_messages(count=5, content_size=500)
        result = manager.compress(messages)

        assert result[-1] == messages[-1]  # 最后 1 条
        assert result[-2] == messages[-2]  # 倒数第 2 条

    def test_middle_compressed_to_single_summary(self) -> None:
        """中间消息压缩为单条摘要消息。"""
        # P0-2: max_tokens=300 使 ratio 落在 HISTORY_SUMMARY（三段式摘要）
        manager = ContextBudgetManager(max_tokens=300, keep_recent=2)
        messages = _make_long_messages(count=5, content_size=500)
        result = manager.compress(messages)

        # head(2) + summary(1) + tail(2) = 5
        assert len(result) == 5
        # 第 3 条是摘要
        summary_msg = result[2]
        assert summary_msg["role"] == "user"
        assert "[系统] 早期上下文摘要" in summary_msg["content"]

    def test_compress_reduces_token_count(self) -> None:
        """压缩后 token 数应显著减少。"""
        manager = ContextBudgetManager(max_tokens=100, keep_recent=2)
        messages = _make_long_messages(count=10, content_size=500)
        before_tokens = ContextBudgetManager.estimate_tokens(messages)

        result = manager.compress(messages)
        after_tokens = ContextBudgetManager.estimate_tokens(result)

        assert after_tokens < before_tokens
        # 应节省至少 50% 的 token
        assert after_tokens < before_tokens * 0.5

    def test_noop_when_too_few_messages(self) -> None:
        """消息数不足时 compress 原样返回。"""
        manager = ContextBudgetManager(max_tokens=10, keep_recent=2)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "user", "content": "recent1"},
            {"role": "user", "content": "recent2"},
        ]
        result = manager.compress(messages)
        assert result == messages

    def test_compressed_message_contains_key_info(self) -> None:
        """摘要消息应包含中间消息的关键信息。"""
        manager = ContextBudgetManager(max_tokens=50, keep_recent=2)
        messages = [
            {"role": "system", "content": _THINK_SYSTEM_STABLE},
            {"role": "user", "content": "用户问题"},
            {"role": "user", "content": "[系统] 已检索到 15 篇文档"},
            {"role": "user", "content": "[系统] 工具结果：订单 BG2024001 金额 5000 元"},
            {"role": "user", "content": "[系统] 工具结果：↑ [见第1轮 search_erp 结果]"},
            {"role": "user", "content": "recent message 1"},
            {"role": "user", "content": "recent message 2"},
        ]
        result = manager.compress(messages)

        summary = result[2]["content"]
        # 检索结果摘要
        assert "检索15篇" in summary
        # 工具结果摘要
        assert "工具:" in summary
        # 指针引用
        assert "重复结果" in summary

    def test_compress_stats_tracked(self) -> None:
        """压缩统计被正确追踪。"""
        manager = ContextBudgetManager(max_tokens=100, keep_recent=2)
        messages = _make_long_messages(count=10, content_size=500)

        manager.compress(messages)
        stats = manager.get_stats()
        assert stats["compress_count"] == 1
        assert stats["total_tokens_saved"] > 0

        # 第二次压缩
        messages2 = _make_long_messages(count=10, content_size=500)
        manager.compress(messages2)
        stats = manager.get_stats()
        assert stats["compress_count"] == 2

    def test_reset_clears_stats(self) -> None:
        """reset 清空统计信息。"""
        manager = ContextBudgetManager(max_tokens=100, keep_recent=2)
        messages = _make_long_messages(count=10, content_size=500)
        manager.compress(messages)
        assert manager.get_stats()["compress_count"] > 0

        manager.reset()
        stats = manager.get_stats()
        assert stats["compress_count"] == 0
        assert stats["total_tokens_saved"] == 0


# ======================================================================
# ContextBudgetManager — _compress_single_message 测试
# ======================================================================


class TestCompressSingleMessage:
    """ContextBudgetManager._compress_single_message 测试。"""

    def test_retrieve_message(self) -> None:
        """检索结果消息压缩为 '检索N篇'。"""
        result = ContextBudgetManager._compress_single_message(
            "[系统] 已检索到 15 篇文档"
        )
        assert "检索15篇" in result

    def test_tool_result_message(self) -> None:
        """工具结果消息压缩为 '工具:前80字'。"""
        long_content = "[系统] 工具结果：" + "订单详情 " * 50
        result = ContextBudgetManager._compress_single_message(long_content)
        assert "工具:" in result

    def test_pointer_reference_message(self) -> None:
        """指针引用消息压缩为 '重复结果(见N轮)'。"""
        result = ContextBudgetManager._compress_single_message(
            "[系统] 工具结果：↑ [见第2轮 search_erp 结果]"
        )
        assert "重复结果" in result
        assert "2" in result

    def test_dynamic_context_message(self) -> None:
        """动态上下文消息压缩为 '第N轮决策'。"""
        result = ContextBudgetManager._compress_single_message(
            "当前状态：迭代 3/5，已有文档 5 篇，工具结果 2 条，请决定下一步。"
        )
        assert "第3轮决策" in result

    def test_empty_content(self) -> None:
        """空内容返回空字符串。"""
        assert ContextBudgetManager._compress_single_message("") == ""

    def test_plain_text_truncated(self) -> None:
        """普通文本截断到最大长度。"""
        long_text = "x" * 200
        result = ContextBudgetManager._compress_single_message(long_text)
        assert len(result) <= 80


# ======================================================================
# Engine 集成测试
# ======================================================================


class TestEngineBudgetIntegration:
    """engine.py 中 ContextBudgetManager 集成测试。"""

    def test_budget_initialized_in_init(self) -> None:
        """AgenticRAGEngine.__init__ 应初始化 _budget。"""
        engine, _ = _make_engine()
        assert engine._budget is not None
        assert isinstance(engine._budget, ContextBudgetManager)

    @pytest.mark.asyncio
    async def test_budget_reset_on_new_answer(self) -> None:
        """answer() 调用时应重置 _budget 统计。"""
        engine, _ = _make_engine()

        # 模拟已有压缩统计
        engine._budget._compress_count = 5
        engine._budget._total_tokens_saved = 1000

        # answer 调用应重置
        tokens = []
        async for chunk in engine.answer("test", "user-1", "session-1"):
            if isinstance(chunk, str):
                tokens.append(chunk)

        stats = engine._budget.get_stats()
        assert stats["compress_count"] == 0
        assert stats["total_tokens_saved"] == 0

    @pytest.mark.asyncio
    async def test_budget_not_triggered_on_short_conversation(self) -> None:
        """短对话（少量迭代）不应触发压缩。"""
        engine, llm = _make_engine(max_iterations=2)
        state = _make_state()

        responses = ["retrieve", "generate"]
        call_idx = 0

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            llm.call_history.append(list(messages))
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        async def mock_retrieve(state, kb_ids):
            state["retrieved_docs"] = [{"content": "文档内容"}]

        engine._retrieve = mock_retrieve

        await engine._run_decision_loop(state)

        # 短对话不应触发压缩
        stats = engine._budget.get_stats()
        assert stats["compress_count"] == 0
        # messages 只有 system + query + 1 条检索结果
        assert len(state["messages"]) == 3

    @pytest.mark.asyncio
    async def test_budget_triggered_on_long_conversation(self) -> None:
        """长对话（多轮迭代 + 大量不同工具结果）应触发压缩。"""
        engine, llm = _make_engine(max_iterations=5)
        # 降低预算阈值以便在测试中触发压缩（生产默认 2000 tok）
        engine._budget = ContextBudgetManager(max_tokens=200, keep_recent=2)
        state = _make_state()

        responses = ["tool_call", "tool_call", "tool_call", "tool_call", "generate"]
        call_idx = 0

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            llm.call_history.append(list(messages))
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        # Mock _tool_call_streaming — 每轮返回不同的长结果（避免被 P1-Opt3 去重）
        # P1-4: 接受 db / user_uuid 参数（签名需与实际方法一致）
        async def mock_tool_call(state, db=None, user_uuid=None):
            i = state["iteration"]
            state["tool_results"].append({
                "tool": f"search_erp_{i}",
                "result": f"第{i}轮不同结果 " + "数据" * 200,  # ~400 chars each
            })
            yield  # 异步生成器需要 yield

        engine._tool_call_streaming = mock_tool_call

        await engine._run_decision_loop(state)

        # 应触发压缩（4 轮不同结果，每轮 ~300 chars 摘要，总 ~1200 chars > 200 tok 阈值）
        stats = engine._budget.get_stats()
        assert stats["compress_count"] > 0
        assert stats["total_tokens_saved"] > 0

    @pytest.mark.asyncio
    async def test_compressed_messages_preserve_head(self) -> None:
        """压缩后 system + query 仍在 messages 前两条。"""
        engine, llm = _make_engine(max_iterations=5)
        state = _make_state()

        responses = ["tool_call", "tool_call", "tool_call", "tool_call", "generate"]
        call_idx = 0

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            llm.call_history.append(list(messages))
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        async def mock_tool_call(state, db=None, user_uuid=None):
            state["tool_results"].append({
                "tool": "search_erp",
                "result": "大量数据 " * 200,
            })
            yield  # 异步生成器需要 yield

        engine._tool_call_streaming = mock_tool_call

        await engine._run_decision_loop(state)

        # 前两条始终是 system + query
        assert state["messages"][0]["role"] == "system"
        assert state["messages"][0]["content"] == _THINK_SYSTEM_STABLE
        assert state["messages"][1]["role"] == "user"
        assert state["messages"][1]["content"] == state["query"]

    @pytest.mark.asyncio
    async def test_compressed_messages_contain_summary(self) -> None:
        """压缩后 messages 中应包含摘要消息。"""
        engine, llm = _make_engine(max_iterations=5)
        # 降低预算阈值以便在测试中触发压缩
        engine._budget = ContextBudgetManager(max_tokens=200, keep_recent=2)
        state = _make_state()

        responses = ["tool_call", "tool_call", "tool_call", "tool_call", "generate"]
        call_idx = 0

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            llm.call_history.append(list(messages))
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        async def mock_tool_call(state, db=None, user_uuid=None):
            i = state["iteration"]
            state["tool_results"].append({
                "tool": f"search_erp_{i}",
                "result": f"第{i}轮不同结果 " + "数据" * 200,
            })
            yield  # 异步生成器需要 yield

        engine._tool_call_streaming = mock_tool_call

        await engine._run_decision_loop(state)

        # 应存在摘要消息
        summary_msgs = [
            m for m in state["messages"]
            if "早期上下文摘要" in m.get("content", "")
        ]
        assert len(summary_msgs) >= 1

    @pytest.mark.asyncio
    async def test_trace_includes_budget_stats(self) -> None:
        """answer() 结束后 trace metadata 包含 budget 统计。"""
        from unittest.mock import patch

        mock_trace = MagicMock()
        mock_trace.start = MagicMock()
        mock_trace.finalize = MagicMock()

        with patch("app.rag.engine.TraceContext", return_value=mock_trace):
            engine, _ = _make_engine(max_iterations=2)
            tokens = []
            async for chunk in engine.answer("test", "user-1", "session-1"):
                if isinstance(chunk, str):
                    tokens.append(chunk)

        # 检查 finalize 被调用且 metadata 包含 budget 字段
        assert mock_trace.finalize.called
        call_args = mock_trace.finalize.call_args
        metadata = call_args.kwargs.get("metadata", {})
        assert "budget_compress_count" in metadata
        assert "budget_tokens_saved" in metadata


# ======================================================================
# 回归测试
# ======================================================================


class TestBackwardCompatibility:
    """回归测试 — 确保 P2 优化不破坏已有功能。"""

    @pytest.mark.asyncio
    async def test_answer_still_yields_tokens(self) -> None:
        """answer() 仍正确 yield token。"""
        engine, _ = _make_engine()
        tokens = []
        async for chunk in engine.answer("test query", "user-1", "session-1"):
            if isinstance(chunk, str):
                tokens.append(chunk)
        assert len(tokens) > 0
        assert "".join(tokens) == "这是答案"

    @pytest.mark.asyncio
    async def test_existing_think_still_works(self) -> None:
        """_think 仍正确返回决策。"""
        engine, llm = _make_engine(llm_response="retrieve")
        state = _make_state()
        state["messages"] = [
            {"role": "system", "content": _THINK_SYSTEM_STABLE},
            {"role": "user", "content": state["query"]},
        ]
        decision = await engine._think(state)
        assert decision == "retrieve"

    @pytest.mark.asyncio
    async def test_existing_reflect_still_works(self) -> None:
        """_reflect 仍正常执行（P1-Opt4 摘要 + P2 不影响 reflect）。"""
        engine, llm = _make_engine(llm_response="satisfied")
        state = _make_state(answer="## 答案\n- 要点1\n- 要点2\n详细内容...")
        await engine._reflect(state)
        # reflect 不修改 state，只记录日志
        assert state["answer"] == "## 答案\n- 要点1\n- 要点2\n详细内容..."

    @pytest.mark.asyncio
    async def test_decision_loop_short_path_no_budget(self) -> None:
        """短路径决策循环（think → generate）不触发预算压缩。"""
        engine, llm = _make_engine(max_iterations=1)
        state = _make_state()

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            llm.call_history.append(list(messages))
            yield "generate"

        llm.chat = mock_chat

        await engine._run_decision_loop(state)

        assert engine._budget.get_stats()["compress_count"] == 0
        # 只有 system + query
        assert len(state["messages"]) == 2

    def test_budget_default_max_tokens(self) -> None:
        """默认 max_tokens 为 2000。"""
        manager = ContextBudgetManager()
        assert manager._max_tokens == 2000

    def test_budget_default_keep_recent(self) -> None:
        """默认 keep_recent 为 2。"""
        manager = ContextBudgetManager()
        assert manager._keep_recent == 2
