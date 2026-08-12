"""方案二（Slot + 硬/软约束）+ 方案三（拒识 + 澄清两出口）单元测试。

覆盖：
- RuleMatcher: 拒识（UNSUPPORTED）规则匹配
- IntentRouter: 拒识意图走终态出口
- LLMIntentParser: missing_slots / constraints 解析 + 白名单过滤
- ShortcutHandler: 拒识/澄清 SSE 事件出口
- ShortcutHandler: 硬约束过滤（密级上限/排除密级/必含关键词）
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.intent.llm_parser import LLMIntentParser
from app.intent.router import (
    IntentConstraints,
    IntentResult,
    IntentRouter,
    IntentType,
    _SHORTCUT_INTENTS,
    _TERMINAL_INTENTS,
)
from app.intent.rule_matcher import RuleMatcher
from app.intent.shortcut_handler import ShortcutHandler
from app.utils.sse import SSEEvent, SSEEventType


# ------------------------------------------------------------------
# 测试双打
# ------------------------------------------------------------------


class FakeLLM:
    """按构造时给定的响应切块返回的假 LLM。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def chat(self, messages, **kwargs: Any) -> Any:
        yield self._payload


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeDB:
    """假 DB — ``execute`` 直接返回构造时给定的密级行。"""

    def __init__(self, cls_map: dict[str, str]) -> None:
        self._cls = cls_map

    async def execute(self, stmt: Any) -> FakeResult:
        return FakeResult(
            [(uuid.UUID(k), v) for k, v in self._cls.items()]
        )


# ------------------------------------------------------------------
# 方案三：拒识规则匹配
# ------------------------------------------------------------------


class TestRuleMatcherRejection:
    def setup_method(self):
        self.matcher = RuleMatcher()

    @pytest.mark.parametrize("query", [
        "帮我订一张机票",
        "明天北京的天气怎么样",
        "推荐一下买哪只股票",
        "讲个笑话听听",
        "订个外卖",
    ])
    def test_unsupported_match(self, query):
        result = self.matcher.match(query)
        assert result is not None
        assert result.intent == IntentType.UNSUPPORTED
        assert result.use_shortcut is True  # 走终态出口

    @pytest.mark.parametrize("query", [
        "搜索报销流程",
        "列出所有文档",
        "查一下微服务架构规范",
    ])
    def test_kb_scope_not_rejected(self, query):
        """企业知识库范围内的查询不应被误拒识。"""
        result = self.matcher.match(query)
        assert result is not None
        assert result.intent != IntentType.UNSUPPORTED


# ------------------------------------------------------------------
# 方案三：拒识意图走终态出口
# ------------------------------------------------------------------


class TestRouterTerminalExit:
    def setup_method(self):
        self.router = IntentRouter(llm_provider=None)

    @pytest.mark.asyncio
    async def test_unsupported_rules_to_terminal(self):
        result = await self.router.route(
            query="帮我订一张机票",
            memory_context="",
            agent_type="qa",
        )
        assert result.intent == IntentType.UNSUPPORTED
        assert result.intent in _TERMINAL_INTENTS
        assert result.use_shortcut is True

    def test_terminal_intents_not_in_shortcut_retrieval(self):
        """终态出口意图不属于检索快捷集合。"""
        assert IntentType.UNSUPPORTED not in _SHORTCUT_INTENTS
        assert IntentType.UNCLEAR not in _SHORTCUT_INTENTS
        assert IntentType.UNSUPPORTED in _TERMINAL_INTENTS
        assert IntentType.UNCLEAR in _TERMINAL_INTENTS


# ------------------------------------------------------------------
# 方案二：LLM 解析 missing_slots / constraints
# ------------------------------------------------------------------


class TestLLMParserSlots:
    async def _parse(self, payload: str) -> IntentResult | None:
        parser = LLMIntentParser(FakeLLM(payload))
        return await parser.parse(query="测试", context="")

    @pytest.mark.asyncio
    async def test_parse_unclear_with_missing_slots(self):
        payload = (
            '{"intent": "unclear", "confidence": 0.9, "parameters": {},'
            ' "missing_slots": ["search_query"],'
            ' "constraints": {"hard": {}, "soft": {}}}'
        )
        result = await self._parse(payload)
        assert result is not None
        assert result.intent == IntentType.UNCLEAR
        assert result.missing_slots == ["search_query"]
        assert result.use_shortcut is True  # 走澄清出口

    @pytest.mark.asyncio
    async def test_parse_constraints(self):
        payload = (
            '{"intent": "rag_search", "confidence": 0.9,'
            ' "parameters": {"search_query": "安全规范"},'
            ' "missing_slots": [],'
            ' "constraints": {"hard": {"classification_max": "public"},'
            ' "soft": {"time_range": "近半年"}}}'
        )
        result = await self._parse(payload)
        assert result is not None
        assert result.constraints is not None
        assert result.constraints.hard == {"classification_max": "public"}
        assert result.constraints.soft == {"time_range": "近半年"}

    @pytest.mark.asyncio
    async def test_constraints_whitelist_filters_unknown_keys(self):
        """LLM 注入未知键/非法密级应被白名单过滤。"""
        payload = (
            '{"intent": "rag_search", "confidence": 0.9, "parameters": {},'
            ' "missing_slots": [],'
            ' "constraints": {"hard": {"classification_max": "supersecret",'
            ' "evil_key": 1}, "soft": {"hack": true}}}'
        )
        result = await self._parse(payload)
        assert result is not None
        if result.constraints is not None:
            assert result.constraints.hard.get("classification_max") is None
            assert "evil_key" not in result.constraints.hard
            assert result.constraints.soft == {}

    @pytest.mark.asyncio
    async def test_low_confidence_terminal_falls_back(self):
        """拒识/澄清置信度不足时回退 COMPLEX_QUERY，避免误拒识。"""
        payload = (
            '{"intent": "unsupported", "confidence": 0.3, "parameters": {},'
            ' "missing_slots": [], "constraints": {}}'
        )
        result = await self._parse(payload)
        assert result is not None
        assert result.use_shortcut is False


# ------------------------------------------------------------------
# 方案三：拒识 / 澄清 SSE 出口
# ------------------------------------------------------------------


class TestShortcutHandlerExits:
    @pytest.mark.asyncio
    async def test_unsupported_emits_rejected_event(self):
        handler = ShortcutHandler()
        intent = IntentResult(
            intent=IntentType.UNSUPPORTED,
            confidence=0.9,
            use_shortcut=True,
        )
        events = [e async for e in handler.handle(intent, "订机票", None, None)]
        rejected = [e for e in events if isinstance(e, SSEEvent)]
        assert any(e.event == SSEEventType.INTENT_REJECTED for e in rejected)
        assert any(e.event == SSEEventType.DONE for e in rejected)

    @pytest.mark.asyncio
    async def test_unclear_emits_clarify_event(self):
        handler = ShortcutHandler()
        intent = IntentResult(
            intent=IntentType.UNCLEAR,
            confidence=0.9,
            missing_slots=["search_query", "time_range"],
            use_shortcut=True,
        )
        events = [e async for e in handler.handle(intent, "查一下文档", None, None)]
        clarify = [
            e for e in events
            if isinstance(e, SSEEvent)
            and e.event == SSEEventType.CLARIFICATION_REQUIRED
        ]
        assert clarify
        data = clarify[0].data
        assert data["intent"] == "unclear"
        assert data["missing_slots"] == ["search_query", "time_range"]
        assert "检索的主题" in data["message"]
        assert "时间范围" in data["message"]


# ------------------------------------------------------------------
# 方案二：硬约束过滤
# ------------------------------------------------------------------


class TestHardConstraints:
    def _candidate(self, doc_id: str, cls: str, content: str, kb: str) -> dict:
        return {
            "doc_id": doc_id,
            "kb_id": kb,
            "content": content,
            "title": f"doc-{doc_id}",
            "score": 0.9,
        }

    def _id(self, n: int) -> str:
        """生成确定性 UUID 字符串（末位为 n）。"""
        return str(uuid.UUID(f"00000000-0000-0000-0000-{n:012d}"))

    @pytest.mark.asyncio
    async def test_classification_max_filters(self):
        handler = ShortcutHandler()
        a, b, c = self._id(1), self._id(2), self._id(3)
        candidates = [
            self._candidate(a, "", "内容", "kb-1"),
            self._candidate(b, "", "内容", "kb-1"),
            self._candidate(c, "", "内容", "kb-1"),
        ]
        db = FakeDB({a: "public", b: "confidential", c: "secret"})
        constraints = IntentConstraints(
            hard={"classification_max": "public"}
        )
        filtered = await handler._apply_hard_constraints(
            candidates, constraints, db, None
        )
        assert [c["doc_id"] for c in filtered] == [a]

    @pytest.mark.asyncio
    async def test_exclude_classifications_filters(self):
        handler = ShortcutHandler()
        a, b = self._id(1), self._id(2)
        candidates = [
            self._candidate(a, "", "内容", "kb-1"),
            self._candidate(b, "", "内容", "kb-1"),
        ]
        db = FakeDB({a: "public", b: "secret"})
        constraints = IntentConstraints(
            hard={"exclude_classifications": ["secret"]}
        )
        filtered = await handler._apply_hard_constraints(
            candidates, constraints, db, None
        )
        assert [c["doc_id"] for c in filtered] == [a]

    @pytest.mark.asyncio
    async def test_mandatory_keywords_filters(self):
        handler = ShortcutHandler()
        a, b = self._id(1), self._id(2)
        candidates = [
            self._candidate(a, "", "报销流程说明", "kb-1"),
            self._candidate(b, "", "请假管理规定", "kb-1"),
        ]
        constraints = IntentConstraints(
            hard={"mandatory_keywords": ["报销"]}
        )
        filtered = await handler._apply_hard_constraints(
            candidates, constraints, FakeDB({a: "public", b: "public"}), None
        )
        assert [c["doc_id"] for c in filtered] == [a]

    @pytest.mark.asyncio
    async def test_kb_scope_filters(self):
        handler = ShortcutHandler()
        a, b = self._id(1), self._id(2)
        candidates = [
            self._candidate(a, "", "内容", "kb-1"),
            self._candidate(b, "", "内容", "kb-2"),
        ]
        constraints = IntentConstraints(
            hard={"kb_ids": ["kb-1"]}
        )
        filtered = await handler._apply_hard_constraints(
            candidates, constraints, FakeDB({a: "public", b: "public"}), None
        )
        assert [c["doc_id"] for c in filtered] == [a]

    @pytest.mark.asyncio
    async def test_missing_classification_fail_closed(self):
        """查不到密级的候选（文档已删）应保守剔除。"""
        handler = ShortcutHandler()
        a, b = self._id(1), self._id(2)
        candidates = [
            self._candidate(a, "", "内容", "kb-1"),
            self._candidate(b, "", "内容", "kb-1"),
        ]
        # 仅 b 有密级记录，a 视为已删/非法
        db = FakeDB({b: "public"})
        constraints = IntentConstraints(
            hard={"classification_max": "public"}
        )
        filtered = await handler._apply_hard_constraints(
            candidates, constraints, db, None
        )
        assert [c["doc_id"] for c in filtered] == [b]


# ------------------------------------------------------------------
# 方案二：软约束提示
# ------------------------------------------------------------------


class TestSoftConstraintHint:
    def test_hint_serialization(self):
        soft = {"time_range": "近半年", "doc_type": "规范"}
        hint = ShortcutHandler._soft_constraint_hint(soft)
        assert "近半年" in hint
        assert "规范" in hint

    def test_empty_hint(self):
        assert ShortcutHandler._soft_constraint_hint({}) == ""