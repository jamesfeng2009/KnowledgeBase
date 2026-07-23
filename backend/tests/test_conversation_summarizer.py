"""
P3-C 对话历史滚动摘要单元测试。

覆盖：
    - 不触发压缩（历史不足阈值）
    - 触发压缩（LLM 成功）
    - LLM 失败降级（截断）
    - 无 LLM 时降级
    - 增量摘要合并
    - 空历史处理
"""

import pytest

from app.context.conversation_summarizer import ConversationSummarizer


class TestConversationSummarizerNoCompress:
    """不触发压缩的场景。"""

    @pytest.mark.asyncio
    async def test_empty_history(self):
        """空历史 → 返回空。"""
        summarizer = ConversationSummarizer(llm=None)
        summary, recent = await summarizer.summarize_if_needed([])
        assert summary == ""
        assert recent == []

    @pytest.mark.asyncio
    async def test_short_history_no_compression(self):
        """短历史（不超过阈值）→ 不压缩。"""
        summarizer = ConversationSummarizer(llm=None, max_tokens=600)
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
        summary, recent = await summarizer.summarize_if_needed(history)
        assert summary == ""  # 无旧摘要
        assert len(recent) == 2  # 全量返回

    @pytest.mark.asyncio
    async def test_short_history_with_existing_summary(self):
        """短历史 + 已有摘要 → 不压缩，保留摘要。"""
        summarizer = ConversationSummarizer(llm=None, max_tokens=600)
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
        summary, recent = await summarizer.summarize_if_needed(
            history, existing_summary="之前聊了天气"
        )
        assert summary == "之前聊了天气"
        assert len(recent) == 2


class TestConversationSummarizerCompress:
    """触发压缩的场景。"""

    @pytest.mark.asyncio
    async def test_llm_compress_success(self):
        """LLM 压缩成功。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "用户讨论了北京天气和限号政策。"

        summarizer = ConversationSummarizer(
            llm=MockLLM(),
            max_tokens=10,  # 极低阈值，强制触发
            retained_tokens=5,
        )
        # 构造超阈值历史
        history = [
            {"role": "user", "content": f"这是第{i}轮对话，内容较长".ljust(50)}
            for i in range(10)
        ]
        summary, recent = await summarizer.summarize_if_needed(history)
        assert "天气" in summary or "限号" in summary or "讨论" in summary
        assert len(recent) < len(history)  # 近期消息少于全量

    @pytest.mark.asyncio
    async def test_llm_compress_with_existing_summary(self):
        """增量摘要合并。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                # 检查 prompt 中包含旧摘要
                content = messages[0]["content"]
                if "已有摘要" in content:
                    yield "综合摘要：天气和限号"
                else:
                    yield "新摘要：限号"

        summarizer = ConversationSummarizer(
            llm=MockLLM(),
            max_tokens=10,
            retained_tokens=5,
        )
        history = [
            {"role": "user", "content": f"消息{i}".ljust(50)}
            for i in range(10)
        ]
        summary, recent = await summarizer.summarize_if_needed(
            history, existing_summary="旧摘要：天气"
        )
        assert "天气" in summary or "限号" in summary

    @pytest.mark.asyncio
    async def test_llm_compress_exception(self):
        """LLM 异常 → 降级为截断。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                raise RuntimeError("API error")
                yield  # make it an async generator

        summarizer = ConversationSummarizer(
            llm=MockLLM(),
            max_tokens=10,
            retained_tokens=5,
        )
        history = [
            {"role": "user", "content": f"消息{i}".ljust(50)}
            for i in range(10)
        ]
        summary, recent = await summarizer.summarize_if_needed(history)
        # 降级：旧摘要 + 第一条消息截断
        assert len(summary) > 0
        assert len(recent) < len(history)

    @pytest.mark.asyncio
    async def test_no_llm_compress(self):
        """无 LLM → 降级为截断。"""
        summarizer = ConversationSummarizer(
            llm=None,
            max_tokens=10,
            retained_tokens=5,
        )
        history = [
            {"role": "user", "content": f"消息{i}".ljust(50)}
            for i in range(10)
        ]
        summary, recent = await summarizer.summarize_if_needed(
            history, existing_summary="旧摘要"
        )
        # 无 LLM：保留旧摘要 + 截断第一条
        assert "旧摘要" in summary
        assert len(recent) < len(history)

    @pytest.mark.asyncio
    async def test_all_messages_in_retained(self):
        """当所有消息都在 retained 范围内时，old_messages 为空。"""
        summarizer = ConversationSummarizer(
            llm=None,
            max_tokens=10,
            retained_tokens=10000,  # 极大值，所有消息都保留
        )
        history = [
            {"role": "user", "content": "短消息1"},
            {"role": "assistant", "content": "短消息2"},
        ]
        summary, recent = await summarizer.summarize_if_needed(history)
        # 所有消息都在 retained 范围，old_messages 为空
        assert summary == ""
        assert len(recent) == 2
