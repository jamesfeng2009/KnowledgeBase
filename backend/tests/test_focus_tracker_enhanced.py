"""P4-C 焦点历史栈增强测试。"""

import pytest

from app.context.focus_tracker import ConversationFocus, TopicTracker


class TestFocusStack:
    """焦点历史栈测试。"""

    @pytest.mark.asyncio
    async def test_focus_stack_push(self):
        """extract_focus 成功提取后，焦点压入栈。"""
        tracker = TopicTracker(llm=None)
        history = [
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "assistant", "content": "晴"},
        ]
        focus = await tracker.extract_focus(history)
        assert focus is not None
        assert len(tracker._focus_stack) == 1
        assert tracker._focus_stack[0] is focus

    @pytest.mark.asyncio
    async def test_focus_stack_size_limit(self):
        """栈大小限制 — 超过 _FOCUS_STACK_SIZE 时丢弃最旧的。"""
        tracker = TopicTracker(llm=None)
        # 手动压入超过限制数量的焦点
        for i in range(tracker._FOCUS_STACK_SIZE + 3):
            focus = ConversationFocus(
                topic=f"topic_{i}", entity=f"entity_{i}", confidence=0.8,
            )
            tracker._push_focus(focus)

        assert len(tracker._focus_stack) == tracker._FOCUS_STACK_SIZE
        # 最旧的被丢弃
        assert tracker._focus_stack[0].topic == "topic_3"
        # 最新的保留
        assert tracker._focus_stack[-1].topic == f"topic_{tracker._FOCUS_STACK_SIZE + 2}"

    @pytest.mark.asyncio
    async def test_get_focus_history(self):
        """get_focus_history 返回最近 N 个焦点。"""
        tracker = TopicTracker(llm=None)
        for i in range(5):
            tracker._push_focus(ConversationFocus(
                topic=f"topic_{i}", entity=f"entity_{i}",
            ))

        history = tracker.get_focus_history(n=3)
        assert len(history) == 3
        assert history[0].topic == "topic_2"
        assert history[1].topic == "topic_3"
        assert history[2].topic == "topic_4"

    @pytest.mark.asyncio
    async def test_get_focus_history_empty(self):
        """空栈时 get_focus_history 返回空列表。"""
        tracker = TopicTracker(llm=None)
        assert tracker.get_focus_history() == []

    def test_reset_focus_clears_stack(self):
        """reset_focus 清空焦点栈。"""
        tracker = TopicTracker(llm=None)
        tracker._push_focus(ConversationFocus(topic="天气", entity="北京"))
        tracker._push_focus(ConversationFocus(topic="限号", entity="上海"))
        assert len(tracker._focus_stack) == 2

        tracker.reset_focus()
        assert len(tracker._focus_stack) == 0

    def test_last_focus_compat_property(self):
        """_last_focus 兼容属性 — 返回栈顶。"""
        tracker = TopicTracker(llm=None)
        assert tracker._last_focus is None

        focus = ConversationFocus(topic="天气", entity="北京")
        tracker._push_focus(focus)
        assert tracker._last_focus is focus

        tracker._push_focus(ConversationFocus(topic="限号", entity="上海"))
        assert tracker._last_focus.topic == "限号"

    @pytest.mark.asyncio
    async def test_reset_clears_stack(self):
        """reset() 清空栈（向后兼容）。"""
        tracker = TopicTracker(llm=None)
        history = [
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "assistant", "content": "晴"},
        ]
        await tracker.extract_focus(history)
        assert tracker._last_focus is not None

        tracker.reset()
        assert tracker._last_focus is None
        assert len(tracker._focus_stack) == 0

    @pytest.mark.asyncio
    async def test_drift_reset_then_new_focus(self):
        """漂移后 reset_focus + 新 extract_focus → 新焦点在空栈中。"""
        tracker = TopicTracker(llm=None)
        history1 = [
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "assistant", "content": "晴"},
        ]
        focus1 = await tracker.extract_focus(history1)
        assert focus1 is not None
        assert focus1.topic == "天气"

        # 模拟漂移 — 清空栈
        tracker.reset_focus()
        assert len(tracker._focus_stack) == 0

        # 新焦点提取
        history2 = [
            {"role": "user", "content": "如何申请报销？"},
            {"role": "assistant", "content": "回复"},
        ]
        focus2 = await tracker.extract_focus(history2)
        assert focus2 is not None
        assert "报销" in focus2.topic
        assert len(tracker._focus_stack) == 1

    @pytest.mark.asyncio
    async def test_multiple_extractions_accumulate(self):
        """多次 extract_focus 调用累积焦点栈。"""
        tracker = TopicTracker(llm=None)
        histories = [
            [
                {"role": "user", "content": "北京天气怎么样？"},
                {"role": "assistant", "content": "晴"},
            ],
            [
                {"role": "user", "content": "北京限号多少？"},
                {"role": "assistant", "content": "3和7"},
            ],
        ]

        for h in histories:
            await tracker.extract_focus(h)

        assert len(tracker._focus_stack) == 2
        assert tracker._focus_stack[0].topic == "天气"
        assert "限号" in tracker._focus_stack[1].topic
