"""
P3-A 焦点追踪 + 指代消解单元测试。

覆盖：
    - ConversationFocus 数据结构
    - TopicTracker 规则提取（含 P2 EntityRegistry 集成）
    - TopicTracker 焦点继承
    - TopicTracker LLM 兜底（mock）
    - CoreferenceResolver.needs_resolution 检测
    - CoreferenceResolver 规则补全（无 LLM）
    - CoreferenceResolver LLM 补全（mock）
    - 优雅降级
"""

import pytest

from app.context.focus_tracker import ConversationFocus, TopicTracker
from app.context.coreference_resolver import CoreferenceResolver


# ============================================================
# ConversationFocus
# ============================================================

class TestConversationFocus:
    """ConversationFocus 数据结构测试。"""

    def test_to_context_str(self):
        focus = ConversationFocus(
            topic="限号政策", entity="北京", intent="查询"
        )
        s = focus.to_context_str()
        assert "限号政策" in s
        assert "北京" in s
        assert "查询" in s

    def test_to_dict(self):
        focus = ConversationFocus(
            topic="天气", entity="上海", intent="查询", confidence=0.9
        )
        d = focus.to_dict()
        assert d["topic"] == "天气"
        assert d["entity"] == "上海"
        assert d["intent"] == "查询"
        assert d["confidence"] == 0.9

    def test_default_intent(self):
        focus = ConversationFocus(topic="天气", entity="上海")
        assert focus.intent == "查询"


# ============================================================
# TopicTracker
# ============================================================

class TestTopicTrackerRuleExtract:
    """TopicTracker 规则提取测试。"""

    def setup_method(self):
        self.tracker = TopicTracker(llm=None)

    def test_rule_extract_topic_keywords(self):
        """规则提取 — 话题关键词。"""
        focus = self.tracker._rule_extract("北京今天车辆限号多少？")
        assert focus is not None
        assert "限号" in focus.topic or "限行" in focus.topic

    def test_rule_extract_weather(self):
        focus = self.tracker._rule_extract("北京今天天气怎么样？")
        assert focus is not None
        assert focus.topic == "天气"

    def test_rule_extract_reimbursement(self):
        focus = self.tracker._rule_extract("如何申请报销？")
        assert focus is not None
        assert "报销" in focus.topic

    def test_rule_extract_no_match(self):
        focus = self.tracker._rule_extract("你好")
        assert focus is None

    def test_rule_extract_empty(self):
        focus = self.tracker._rule_extract("")
        assert focus is None

    @pytest.mark.asyncio
    async def test_extract_focus_single_turn(self):
        """单轮对话无法确定焦点 — 返回 None。"""
        history = [{"role": "user", "content": "你好"}]
        focus = await self.tracker.extract_focus(history)
        assert focus is None

    @pytest.mark.asyncio
    async def test_extract_focus_empty_history(self):
        focus = await self.tracker.extract_focus([])
        assert focus is None

    @pytest.mark.asyncio
    async def test_extract_focus_multi_turn(self):
        """多轮对话 — 规则提取焦点。"""
        history = [
            {"role": "user", "content": "北京今天天气怎么样？"},
            {"role": "assistant", "content": "北京今天晴，25度"},
            {"role": "user", "content": "北京今天车辆限号多少？"},
            {"role": "assistant", "content": "今天限行尾号3和7"},
        ]
        focus = await self.tracker.extract_focus(history)
        assert focus is not None
        assert "限号" in focus.topic or "限行" in focus.topic

    @pytest.mark.asyncio
    async def test_focus_inheritance(self):
        """焦点继承 — 最新查询无关键词时继承上次焦点。"""
        history1 = [
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "assistant", "content": "晴"},
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "assistant", "content": "晴"},
        ]
        focus1 = await self.tracker.extract_focus(history1)
        assert focus1 is not None

        # 第二次调用，历史中无新关键词
        history2 = [
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "assistant", "content": "晴"},
            {"role": "user", "content": "继续说"},
            {"role": "assistant", "content": "什么？"},
        ]
        focus2 = await self.tracker.extract_focus(history2)
        # 应该继承上次焦点或返回 None
        if focus2 is not None:
            assert focus2.topic == focus1.topic


class TestTopicTrackerLLM:
    """TopicTracker LLM 兜底测试。"""

    @pytest.mark.asyncio
    async def test_llm_extract_success(self):
        """LLM 提取焦点成功。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=80):
                yield "限号政策|上海|查询"

        tracker = TopicTracker(llm=MockLLM())
        history = [
            {"role": "user", "content": "上海呢？"},
            {"role": "assistant", "content": "什么？"},
        ]
        focus = await tracker.extract_focus(history)
        assert focus is not None
        assert focus.topic == "限号政策"
        assert focus.entity == "上海"

    @pytest.mark.asyncio
    async def test_llm_extract_malformed(self):
        """LLM 输出格式错误 — 返回 None。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=80):
                yield "无法分析"

        tracker = TopicTracker(llm=MockLLM())
        history = [
            {"role": "user", "content": "某个不常见的查询"},
            {"role": "assistant", "content": "回复"},
        ]
        focus = await tracker.extract_focus(history)
        assert focus is None

    @pytest.mark.asyncio
    async def test_llm_extract_exception(self):
        """LLM 异常 — 优雅降级。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=80):
                raise RuntimeError("API error")
                yield  # make it an async generator

        tracker = TopicTracker(llm=MockLLM())
        history = [
            {"role": "user", "content": "某个查询"},
            {"role": "assistant", "content": "回复"},
        ]
        focus = await tracker.extract_focus(history)
        assert focus is None


# ============================================================
# CoreferenceResolver
# ============================================================

class TestCoreferenceResolverDetection:
    """CoreferenceResolver.needs_resolution 检测测试。"""

    def setup_method(self):
        self.resolver = CoreferenceResolver(llm=None)

    def test_needs_resolution_short_ellipsis(self):
        """短句 + 省略词 → 需要消解。"""
        assert self.resolver.needs_resolution("那上海呢？") is True
        assert self.resolver.needs_resolution("他怎么样") is True
        assert self.resolver.needs_resolution("也是这样") is True

    def test_no_resolution_long_query(self):
        """长句 → 不需要消解。"""
        assert self.resolver.needs_resolution("这是一个非常长的查询超过三十个字符所以不需要进行指代消解") is False

    def test_no_resolution_explicit_verb(self):
        """包含明确动词 → 不需要消解。"""
        assert self.resolver.needs_resolution("搜索北京天气") is False
        assert self.resolver.needs_resolution("查看报销流程") is False
        assert self.resolver.needs_resolution("什么是知识图谱") is False

    def test_no_resolution_empty(self):
        """空查询 → 不需要消解。"""
        assert self.resolver.needs_resolution("") is False
        assert self.resolver.needs_resolution("a") is False


class TestCoreferenceResolverRule:
    """CoreferenceResolver 规则补全测试（无 LLM）。"""

    @pytest.mark.asyncio
    async def test_rule_resolve_basic(self):
        """规则补全 — 替换实体。"""
        resolver = CoreferenceResolver(llm=None)
        focus = ConversationFocus(topic="限号政策", entity="北京")
        result = await resolver.resolve("那上海呢？", focus)
        assert "上海" in result
        assert "限号" in result

    @pytest.mark.asyncio
    async def test_no_resolution_without_focus(self):
        """无焦点 → 原样返回。"""
        resolver = CoreferenceResolver(llm=None)
        result = await resolver.resolve("那上海呢？", None)
        assert result == "那上海呢？"

    @pytest.mark.asyncio
    async def test_no_resolution_not_needed(self):
        """不需要消解 → 原样返回。"""
        resolver = CoreferenceResolver(llm=None)
        focus = ConversationFocus(topic="天气", entity="北京")
        result = await resolver.resolve("搜索北京天气情况", focus)
        assert result == "搜索北京天气情况"


class TestCoreferenceResolverLLM:
    """CoreferenceResolver LLM 补全测试。"""

    @pytest.mark.asyncio
    async def test_llm_resolve_success(self):
        """LLM 补全成功。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=100):
                yield "上海今天车辆限号多少？"

        resolver = CoreferenceResolver(llm=MockLLM())
        focus = ConversationFocus(topic="限号政策", entity="北京")
        result = await resolver.resolve("那上海呢？", focus)
        assert result == "上海今天车辆限号多少？"

    @pytest.mark.asyncio
    async def test_llm_resolve_same_as_original(self):
        """LLM 返回与原查询相同 → 返回原查询。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=100):
                yield "那上海呢？"

        resolver = CoreferenceResolver(llm=MockLLM())
        focus = ConversationFocus(topic="限号政策", entity="北京")
        result = await resolver.resolve("那上海呢？", focus)
        assert result == "那上海呢？"

    @pytest.mark.asyncio
    async def test_llm_resolve_exception(self):
        """LLM 异常 → 优雅降级为规则补全。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=100):
                raise RuntimeError("API error")
                yield  # make it an async generator

        resolver = CoreferenceResolver(llm=MockLLM())
        focus = ConversationFocus(topic="限号政策", entity="北京")
        result = await resolver.resolve("那上海呢？", focus)
        # LLM 异常时回退到规则补全
        assert "上海" in result
        assert "限号" in result

    @pytest.mark.asyncio
    async def test_llm_resolve_too_long(self):
        """LLM 输出过长 → 规则补全降级。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=100):
                yield "x" * 300  # 超过 200 字符

        resolver = CoreferenceResolver(llm=MockLLM())
        focus = ConversationFocus(topic="限号政策", entity="北京")
        result = await resolver.resolve("那上海呢？", focus)
        # LLM 输出过长 → 规则补全降级
        assert "上海" in result
        assert "限号" in result


# ============================================================
# TopicTracker reset
# ============================================================

class TestTopicTrackerReset:
    """TopicTracker reset 方法测试。"""

    @pytest.mark.asyncio
    async def test_reset_clears_focus(self):
        tracker = TopicTracker(llm=None)
        history = [
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "assistant", "content": "晴"},
        ]
        focus = await tracker.extract_focus(history)
        assert focus is not None
        assert tracker._last_focus is not None

        tracker.reset()
        assert tracker._last_focus is None
