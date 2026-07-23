"""P4-C 指代消解增强测试 — 历史注入 + 焦点栈。"""

import pytest

from app.context.coreference_resolver import CoreferenceResolver
from app.context.focus_tracker import ConversationFocus


class PromptCapturingLLM:
    """Mock LLM — 捕获 prompt 并返回预设响应。"""

    def __init__(self, response: str = "补全后的查询"):
        self._response = response
        self.captured_prompt: str = ""

    async def chat(self, messages, stream=True, max_tokens=100):
        self.captured_prompt = messages[0].get("content", "")
        yield self._response


class FailingLLM:
    """Mock LLM — 总是抛异常。"""

    async def chat(self, messages, stream=True, max_tokens=100):
        raise RuntimeError("API error")
        yield  # make it an async generator


class TestCoreferenceHistoryInjection:
    """历史注入测试。"""

    @pytest.mark.asyncio
    async def test_history_injected_into_prompt(self):
        """传入 history 时，LLM prompt 包含历史内容。"""
        llm = PromptCapturingLLM(response="上海今天限号多少？")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")
        history = [
            {"role": "user", "content": "北京今天限号多少？"},
            {"role": "assistant", "content": "今天限行尾号3和7"},
        ]

        await resolver.resolve("那上海呢？", focus, history=history)

        assert "北京今天限号" in llm.captured_prompt
        assert "限行尾号3和7" in llm.captured_prompt

    @pytest.mark.asyncio
    async def test_history_none_no_injection(self):
        """history=None 时，prompt 中历史部分为"（无）"。"""
        llm = PromptCapturingLLM(response="上海限号政策")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")

        await resolver.resolve("那上海呢？", focus, history=None)

        assert "（无）" in llm.captured_prompt

    @pytest.mark.asyncio
    async def test_history_empty_list(self):
        """history=[] 时，prompt 中历史部分为"（无）"。"""
        llm = PromptCapturingLLM(response="上海限号政策")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")

        await resolver.resolve("那上海呢？", focus, history=[])

        assert "（无）" in llm.captured_prompt


class TestCoreferenceFocusStackInjection:
    """焦点栈注入测试。"""

    @pytest.mark.asyncio
    async def test_focus_stack_injected_into_prompt(self):
        """传入 focus_stack 时，LLM prompt 包含焦点历史。"""
        llm = PromptCapturingLLM(response="上海限号政策")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")
        focus_stack = [
            ConversationFocus(topic="天气", entity="北京"),
            ConversationFocus(topic="限号政策", entity="北京"),
        ]

        await resolver.resolve("那上海呢？", focus, focus_stack=focus_stack)

        assert "天气" in llm.captured_prompt
        assert "轮0" in llm.captured_prompt
        assert "轮1" in llm.captured_prompt

    @pytest.mark.asyncio
    async def test_focus_stack_none_no_injection(self):
        """focus_stack=None 时，prompt 只包含当前焦点。"""
        llm = PromptCapturingLLM(response="上海限号政策")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")

        await resolver.resolve("那上海呢？", focus, focus_stack=None)

        assert "限号政策" in llm.captured_prompt
        assert "轮0" not in llm.captured_prompt


class TestCoreferenceEnhanced:
    """增强指代消解综合测试。"""

    @pytest.mark.asyncio
    async def test_multi_turn_coreference(self):
        """多轮跨指代 — 焦点栈 + 历史提供足够上下文。"""
        llm = PromptCapturingLLM(response="上海今天的天气怎么样？")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="天气", entity="北京")
        history = [
            {"role": "user", "content": "北京今天天气怎么样？"},
            {"role": "assistant", "content": "北京今天晴，25度"},
            {"role": "user", "content": "那上海呢？"},
        ]
        focus_stack = [
            ConversationFocus(topic="天气", entity="北京"),
        ]

        result = await resolver.resolve(
            "那上海呢？", focus, history=history, focus_stack=focus_stack,
        )

        assert result == "上海今天的天气怎么样？"
        # 验证 prompt 同时包含历史和焦点栈
        assert "北京今天天气" in llm.captured_prompt
        assert "轮0" in llm.captured_prompt

    @pytest.mark.asyncio
    async def test_backward_compat_no_history_no_stack(self):
        """不传 history/focus_stack 时，与 P3 行为一致。"""
        llm = PromptCapturingLLM(response="上海今天车辆限号多少？")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")

        result = await resolver.resolve("那上海呢？", focus)

        assert result == "上海今天车辆限号多少？"
        # prompt 仍有 focus 和 query
        assert "限号政策" in llm.captured_prompt
        assert "那上海呢" in llm.captured_prompt

    @pytest.mark.asyncio
    async def test_llm_exception_rule_fallback(self):
        """LLM 异常时回退到规则补全。"""
        resolver = CoreferenceResolver(llm=FailingLLM())
        focus = ConversationFocus(topic="限号政策", entity="北京")
        history = [
            {"role": "user", "content": "北京今天限号多少？"},
            {"role": "assistant", "content": "3和7"},
        ]

        result = await resolver.resolve("那上海呢？", focus, history=history)

        # 规则补全：entity_in_query="上海" + focus.topic="限号政策"
        assert "上海" in result
        assert "限号" in result

    @pytest.mark.asyncio
    async def test_rule_resolve_without_llm(self):
        """无 LLM 时规则补全 — history 参数不影响规则路径。"""
        resolver = CoreferenceResolver(llm=None)
        focus = ConversationFocus(topic="限号政策", entity="北京")

        result = await resolver.resolve(
            "那上海呢？", focus,
            history=[{"role": "user", "content": "北京限号"}],
            focus_stack=[focus],
        )

        assert "上海" in result
        assert "限号" in result

    @pytest.mark.asyncio
    async def test_history_truncated_to_6(self):
        """历史注入最多取最近 6 条。"""
        llm = PromptCapturingLLM(response="补全")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")
        # 8 条历史
        history = [
            {"role": "user", "content": "消息0"},
            {"role": "assistant", "content": "回复0"},
            {"role": "user", "content": "消息1"},
            {"role": "assistant", "content": "回复1"},
            {"role": "user", "content": "消息2"},
            {"role": "assistant", "content": "回复2"},
            {"role": "user", "content": "消息最新"},
            {"role": "assistant", "content": "回复最新"},
        ]

        await resolver.resolve("那上海呢？", focus, history=history)

        # 最近 6 条 = 消息2 到 回复最新
        assert "消息最新" in llm.captured_prompt
        assert "回复最新" in llm.captured_prompt
        # 消息0 被截断
        assert "消息0" not in llm.captured_prompt

    @pytest.mark.asyncio
    async def test_focus_stack_truncated_to_3(self):
        """焦点栈注入最多取最近 3 个。"""
        llm = PromptCapturingLLM(response="补全")
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")
        focus_stack = [
            ConversationFocus(topic="topic_0", entity="e0"),
            ConversationFocus(topic="topic_1", entity="e1"),
            ConversationFocus(topic="topic_2", entity="e2"),
            ConversationFocus(topic="topic_3", entity="e3"),
        ]

        await resolver.resolve("那上海呢？", focus, focus_stack=focus_stack)

        # 最近 3 个 = topic_1, topic_2, topic_3
        assert "topic_1" in llm.captured_prompt
        assert "topic_3" in llm.captured_prompt
        # topic_0 被截断
        assert "topic_0" not in llm.captured_prompt
