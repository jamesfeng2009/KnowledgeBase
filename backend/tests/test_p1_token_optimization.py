"""P1 Token 优化测试 — 跨轮去重 / Reflect 摘要 / 历史窗口化 + L1 注入。

覆盖三个 P1 优化点：
- P1-Opt3: CrossTurnDeduplicator — 跨轮工具结果去重，重复结果用指针引用替代
- P1-Opt4: _reflect 传答案摘要而非全文，_summarize_for_reflect 提取要点
- P1-Opt5: ChatService 历史窗口化 + MemoryContext.to_system_prompt 渲染 L1
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.context_dedup import CrossTurnDeduplicator, ToolResultRef
from app.rag.engine import AgenticRAGEngine, _THINK_SYSTEM_STABLE


# ======================================================================
# Mock 实现（复用 test_token_optimization.py 的 Mock）
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

    async def generate(self, query, retrieved_docs, tool_results, memory_context=""):
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
    # 禁用自动创建的 PlanManager：本测试组聚焦 think/工具结果去重，
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


# ======================================================================
# P1-Opt3: CrossTurnDeduplicator 测试
# ======================================================================


class TestCrossTurnDeduplicator:
    """P1-Opt3: 跨轮工具结果去重器测试。"""

    def test_first_registration_returns_full_summary(self) -> None:
        """首次注册的工具结果应返回完整摘要。"""
        dedup = CrossTurnDeduplicator()
        result = dedup.register(
            turn=1, tool_name="search_erp",
            result_content="订单 BG2024001 金额 5000 元 状态已审批",
        )
        assert "订单 BG2024001" in result
        assert "见第" not in result  # 不是指针引用
        assert dedup.get_seen_count() == 1

    def test_duplicate_returns_pointer_reference(self) -> None:
        """重复的工具结果应返回指针引用。"""
        dedup = CrossTurnDeduplicator()
        content = "订单 BG2024001 金额 5000 元 状态已审批"
        dedup.register(turn=1, tool_name="search_erp", result_content=content)

        # 完全相同的内容
        result = dedup.register(turn=3, tool_name="search_erp", result_content=content)
        assert "↑" in result
        assert "见第1轮" in result
        assert "search_erp" in result
        assert dedup.get_seen_count() == 1  # 没有新增

    def test_similar_content_returns_pointer(self) -> None:
        """高度相似的内容应返回指针引用。"""
        dedup = CrossTurnDeduplicator()
        dedup.register(
            turn=1, tool_name="search_erp",
            result_content="订单 BG2024001 金额 5000 元 状态已审批 时间 2024-03-15 备注 无",
        )
        # 大部分词相同，仅末尾日期不同
        result = dedup.register(
            turn=2, tool_name="search_erp",
            result_content="订单 BG2024001 金额 5000 元 状态已审批 时间 2024-03-16 备注 无",
        )
        assert "见第1轮" in result

    def test_different_content_returns_full_summary(self) -> None:
        """完全不同的内容应返回完整摘要。"""
        dedup = CrossTurnDeduplicator()
        dedup.register(turn=1, tool_name="search_erp",
                       result_content="订单 BG2024001 金额 5000 元")
        result = dedup.register(
            turn=2, tool_name="search_oa",
            result_content="审批流程 报销单 审批人 张三 部门 销售部",
        )
        assert "见第" not in result
        assert "审批流程" in result
        assert dedup.get_seen_count() == 2

    def test_reset_clears_seen_list(self) -> None:
        """reset 应清空已见列表。"""
        dedup = CrossTurnDeduplicator()
        dedup.register(turn=1, tool_name="search_erp", result_content="结果A")
        assert dedup.get_seen_count() == 1

        dedup.reset()
        assert dedup.get_seen_count() == 0

        # reset 后相同内容不再被去重
        result = dedup.register(turn=1, tool_name="search_erp", result_content="结果A")
        assert "见第" not in result

    def test_empty_content_not_deduped(self) -> None:
        """空内容不应被去重。"""
        dedup = CrossTurnDeduplicator()
        result = dedup.register(turn=1, tool_name="search", result_content="")
        assert result == ""

    def test_tool_result_ref_to_ref_string(self) -> None:
        """ToolResultRef.to_ref_string 应生成正确的指针引用。"""
        ref = ToolResultRef(turn=2, tool_name="search_crm", summary="客户信息")
        ref_str = ref.to_ref_string()
        assert "第2轮" in ref_str
        assert "search_crm" in ref_str
        assert "↑" in ref_str

    def test_different_threshold(self) -> None:
        """自定义相似度阈值应生效。"""
        dedup = CrossTurnDeduplicator(similarity_threshold=0.99)
        dedup.register(turn=1, tool_name="search",
                       result_content="A B C D E")
        # 5 个词中 4 个相同，Jaccard = 4/6 = 0.67 < 0.99
        result = dedup.register(
            turn=2, tool_name="search",
            result_content="A B C D F",
        )
        # 阈值很高，不应被去重
        assert "见第" not in result

    def test_summary_truncated(self) -> None:
        """超长结果摘要应被截断。"""
        dedup = CrossTurnDeduplicator()
        long_content = "A" * 500
        result = dedup.register(turn=1, tool_name="search", result_content=long_content)
        assert len(result) <= 300  # _SUMMARY_MAX_CHARS


# ======================================================================
# P1-Opt3: Engine 集成测试
# ======================================================================


class TestEngineDedupIntegration:
    """P1-Opt3: Engine 中去重器集成测试。"""

    @pytest.mark.asyncio
    async def test_dedup_reset_on_new_answer(self) -> None:
        """每次 answer 调用应重置去重器。"""
        engine, llm = _make_engine(llm_response="generate")

        # 模拟已有去重记录
        engine._dedup.register(turn=1, tool_name="search", result_content="test")
        assert engine._dedup.get_seen_count() == 1

        # answer 调用应重置
        tokens = []
        async for chunk in engine.answer("test", "user-1", "session-1"):
            if isinstance(chunk, str):
                tokens.append(chunk)

        assert engine._dedup.get_seen_count() == 0

    @pytest.mark.asyncio
    async def test_dedup_in_decision_loop(self) -> None:
        """_run_decision_loop 中 tool_call 后应使用去重。"""
        engine, llm = _make_engine(max_iterations=3)
        state = _make_state()

        responses = ["tool_call", "tool_call", "generate"]
        call_idx = 0

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            llm.call_history.append(list(messages))
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        # Mock _tool_call_streaming 返回相同结果（异步生成器）
        # P1-4: 接受 db / user_uuid 参数（签名需与实际方法一致）
        async def mock_tool_call_streaming(state, db=None, user_uuid=None):
            state["tool_results"].append({
                "tool": "search_erp",
                "result": "订单 BG2024001 金额 5000 元 状态已审批",
            })
            yield  # 异步生成器需要 yield

        engine._tool_call_streaming = mock_tool_call_streaming

        await engine._run_decision_loop(state)

        # 两次 tool_call 结果相同，第二次应被去重
        tool_messages = [
            m for m in state["messages"]
            if m["role"] == "user" and "[系统] 工具结果" in m.get("content", "")
        ]
        assert len(tool_messages) == 2
        # 第一次是完整摘要
        assert "订单 BG2024001" in tool_messages[0]["content"]
        # 第二次是指针引用
        assert "见第" in tool_messages[1]["content"]


# ======================================================================
# P1-Opt4: Reflect 摘要测试
# ======================================================================


class TestReflectSummary:
    """P1-Opt4: _reflect 传摘要而非全文测试。"""

    def test_summarize_for_reflect_with_key_points(self) -> None:
        """有结构化要点的答案应提取要点 + 引言。"""
        answer = (
            "报销流程如下：\n"
            "1. 填写报销单\n"
            "2. 部门主管审批\n"
            "3. 财务审核\n"
            "4. 打款到工资卡\n"
            "注意事项：请保留发票原件。"
        )
        summary = AgenticRAGEngine._summarize_for_reflect(answer)
        assert "报销流程如下" in summary  # 引言
        assert "填写报销单" in summary  # 要点 1
        assert "部门主管审批" in summary  # 要点 2
        assert "财务审核" in summary  # 要点 3
        assert len(summary) < len(answer)  # 摘要更短

    def test_summarize_for_reflect_with_bullet_points(self) -> None:
        """以 - / • / * 开头的行应被识别为要点。"""
        answer = (
            "以下是注意事项：\n"
            "- 保留发票\n"
            "- 30天内提交\n"
            "- 金额超过5000需VP审批\n"
            "- 跨部门费用需双签\n"
        )
        summary = AgenticRAGEngine._summarize_for_reflect(answer)
        assert "保留发票" in summary
        assert "30天" in summary
        assert "5000" in summary
        # 只保留前 3 个要点
        assert "跨部门" not in summary

    def test_summarize_for_reflect_no_key_points(self) -> None:
        """无结构化要点的答案应截断首段。"""
        answer = "这是一个关于报销流程的详细说明，" + "内容" * 200
        summary = AgenticRAGEngine._summarize_for_reflect(answer)
        assert len(summary) <= 700  # max_chars
        assert summary.startswith("这是一个关于报销流程")

    def test_summarize_for_reflect_empty(self) -> None:
        """空答案应返回空字符串。"""
        assert AgenticRAGEngine._summarize_for_reflect("") == ""

    def test_summarize_for_reflect_max_chars(self) -> None:
        """摘要应不超过 max_chars 限制。"""
        answer = "\n".join([f"- 要点{i} " * 20 for i in range(20)])
        summary = AgenticRAGEngine._summarize_for_reflect(answer, max_chars=100)
        assert len(summary) <= 100

    @pytest.mark.asyncio
    async def test_reflect_sends_summary_not_full(self) -> None:
        """_reflect 降级路径（无 quality_guard）应发送摘要而非完整答案给 LLM。"""
        engine, llm = _make_engine(llm_response="satisfied")
        # 禁用 quality_guard，测试 inline 降级路径
        engine._quality_guard = None
        long_answer = "这是答案。" + "详细内容。" * 200  # 非常长的答案

        state = _make_state(answer=long_answer, iteration=1)
        await engine._reflect(state)

        # LLM 应收到摘要而非全文
        assert len(llm.call_history) == 1
        sent_messages = llm.call_history[0]

        # 找到包含答案的消息
        answer_msg = sent_messages[-1]["content"]
        assert "答案摘要" in answer_msg
        # 摘要应远短于完整答案
        assert len(answer_msg) < len(long_answer)

    @pytest.mark.asyncio
    async def test_reflect_error_handled(self) -> None:
        """_reflect LLM 异常应被捕获，不中断主流程。"""
        class ErrorLLM:
            async def chat(self, *args, **kwargs):
                raise RuntimeError("LLM unavailable")
                yield  # noqa

        engine = AgenticRAGEngine(
            llm=ErrorLLM(),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )
        # 禁用 quality_guard，测试 inline 降级路径的错误处理
        engine._quality_guard = None
        state = _make_state(answer="test answer", iteration=1)
        # 不应抛出异常
        await engine._reflect(state)


# ======================================================================
# P1-Opt5: MemoryContext L1 渲染测试
# ======================================================================


class TestMemoryContextL1Injection:
    """P1-Opt5: MemoryContext.to_system_prompt L1 短期窗口渲染测试。"""

    def test_render_short_term_false_no_l1(self) -> None:
        """render_short_term=False 时不渲染 L1（向后兼容）。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.short_term = [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
        ]
        prompt = ctx.to_system_prompt(render_short_term=False)
        assert "近期对话" not in prompt
        assert "之前的问题" not in prompt

    def test_render_short_term_true_renders_l1(self) -> None:
        """render_short_term=True 时应渲染 L1 短期窗口。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.short_term = [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
        ]
        prompt = ctx.to_system_prompt(render_short_term=True)
        assert "近期对话" in prompt
        assert "之前的问题" in prompt
        assert "之前的回答" in prompt

    def test_short_term_truncated_to_inject_size(self) -> None:
        """L1 注入应只取最近 8 条消息。"""
        from app.memory.memory_manager import MemoryContext, _SHORT_TERM_INJECT_SIZE

        ctx = MemoryContext()
        ctx.short_term = [
            {"role": "user", "content": f"消息{i}"} for i in range(20)
        ]
        prompt = ctx.to_system_prompt(render_short_term=True)

        # 最近的消息应在 prompt 中
        assert f"消息{19}" in prompt
        assert f"消息{12}" in prompt  # 20 - 8 = 12
        # 更早的消息不应在 prompt 中
        assert f"消息{11}" not in prompt

    def test_short_term_message_truncated(self) -> None:
        """每条 L1 消息应截断到 200 字符。"""
        from app.memory.memory_manager import MemoryContext, _SHORT_TERM_MSG_MAX_CHARS

        ctx = MemoryContext()
        long_content = "A" * 500
        ctx.short_term = [{"role": "user", "content": long_content}]
        prompt = ctx.to_system_prompt(render_short_term=True)

        # 截断后的内容应在 prompt 中
        assert "A" * _SHORT_TERM_MSG_MAX_CHARS in prompt
        # 超出部分不应在
        assert "A" * (_SHORT_TERM_MSG_MAX_CHARS + 1) not in prompt

    def test_l3_preferences_limited_to_top3(self) -> None:
        """L3 用户偏好应只注入 top-3。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.user_facts = [
            {"fact_text": f"偏好{i}", "category": "preference"}
            for i in range(10)
        ]
        prompt = ctx.to_system_prompt()
        assert "偏好0" in prompt
        assert "偏好1" in prompt
        assert "偏好2" in prompt
        assert "偏好3" not in prompt  # 只保留 top-3

    def test_l3_summaries_limited_to_top3(self) -> None:
        """L3 历史摘要应只注入 top-3。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.user_facts = [
            {"fact_text": f"摘要{i}", "category": "summary"}
            for i in range(10)
        ]
        prompt = ctx.to_system_prompt()
        assert "摘要0" in prompt
        assert "摘要2" in prompt
        assert "摘要3" not in prompt

    def test_empty_short_term_no_l1_section(self) -> None:
        """L1 为空时不应渲染近期对话部分。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.short_term = []
        prompt = ctx.to_system_prompt(render_short_term=True)
        assert "近期对话" not in prompt

    def test_backward_compatible_default(self) -> None:
        """不传 render_short_term 时默认 False（向后兼容）。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.short_term = [{"role": "user", "content": "test"}]
        prompt = ctx.to_system_prompt()
        assert "近期对话" not in prompt


# ======================================================================
# P1-Opt5: ChatService 历史窗口化测试
# ======================================================================


class TestChatServiceWindowing:
    """P1-Opt5: ChatService 历史消息窗口化测试。"""

    @pytest.mark.asyncio
    async def test_uses_memory_ctx_short_term(self) -> None:
        """有 memory_ctx.short_term 时应使用它，不从 DB 加载。"""
        from app.memory.memory_manager import MemoryContext
        from app.services.chat_service import ChatService

        # Mock 依赖
        mock_msg_repo = AsyncMock()

        service = ChatService.__new__(ChatService)
        service.msg_repo = mock_msg_repo
        service.llm = AsyncMock()
        service.memory = AsyncMock()

        memory_ctx = MemoryContext()
        memory_ctx.short_term = [
            {"role": "user", "content": "历史问题1"},
            {"role": "assistant", "content": "历史回答1"},
        ]
        # Mock to_system_prompt 返回包含 short_term 内容的字符串
        memory_ctx.to_system_prompt = lambda render_short_term=True: "记忆上下文：历史问题1"

        result = await service._build_engine_memory_context(
            "conv-1", "qa", memory_ctx
        )

        # 结果应包含 short_term 内容
        assert "历史问题1" in result
        # msg_repo.get_by_conversation 不应被调用
        mock_msg_repo.get_by_conversation.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_db_with_limit(self) -> None:
        """无 memory_ctx.short_term 时应从 DB 加载但带 limit。"""
        from app.services.chat_service import ChatService

        mock_msg_repo = AsyncMock()
        mock_msg_repo.get_by_conversation.return_value = []

        service = ChatService.__new__(ChatService)
        service.msg_repo = mock_msg_repo
        service.llm = AsyncMock()
        service.memory = AsyncMock()

        await service._build_engine_memory_context("conv-1", "qa", None)

        # 应调用 msg_repo.get_by_conversation 且带 limit 参数
        mock_msg_repo.get_by_conversation.assert_called_once()
        call_kwargs = mock_msg_repo.get_by_conversation.call_args
        assert call_kwargs.kwargs.get("limit") == 16 or "limit" in str(call_kwargs)


# ======================================================================
# 回归测试
# ======================================================================


class TestBackwardCompatibility:
    """回归测试 — 确保 P1 优化不破坏原有功能。"""

    @pytest.mark.asyncio
    async def test_answer_still_yields_tokens(self) -> None:
        """answer 主入口应正常流式输出。"""
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
    async def test_existing_reflect_test_still_works(self) -> None:
        """原有 reflect 测试应仍然通过。"""
        from tests.test_rag_engine import FakeLLM as OriginalFakeLLM

        engine = AgenticRAGEngine(
            llm=OriginalFakeLLM("satisfied"),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )
        state = _make_state(answer="测试答案", iteration=1)

        # 不应抛出异常
        await engine._reflect(state)

    @pytest.mark.asyncio
    async def test_think_still_works(self) -> None:
        """_think 应正常工作。"""
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

    def test_memory_context_to_system_prompt_default(self) -> None:
        """MemoryContext.to_system_prompt() 不传参时应向后兼容。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.user_facts = [{"fact_text": "偏好A", "category": "preference"}]
        prompt = ctx.to_system_prompt()
        assert "偏好A" in prompt
