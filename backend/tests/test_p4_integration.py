"""P4 集成测试 — chat_service 与 P4 检测器集成。

验证 prepare_chat 中的 P4 检测器集成和 stream_chat 中的 SSE 事件推送。
"""

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from app.services.chat_service import PreparedChat
from app.utils.sse import SSEEventType


class TestPreparedChatP4Fields:
    """PreparedChat P4 字段测试。"""

    def test_prepared_chat_has_p4_fields(self):
        """PreparedChat 包含所有 P4 字段且默认值为 None。"""
        pc = PreparedChat(
            query="测试",
            conversation_id=uuid4(),
            agent_type="qa",
            tenant_id=None,
            memory_context="ctx",
            resolved_model_id="",
            default_model_id="",
        )
        assert pc.drift_info is None
        assert pc.preference_overrides is None
        assert pc.repetition_info is None
        assert pc.contradiction_task is None

    def test_prepared_chat_with_p4_data(self):
        """PreparedChat 可携带 P4 检测结果。"""
        pc = PreparedChat(
            query="测试",
            conversation_id=uuid4(),
            agent_type="qa",
            tenant_id=None,
            memory_context="ctx",
            resolved_model_id="",
            default_model_id="",
            drift_info={"is_drift": True, "drift_score": 0.8},
            preference_overrides={"preference_type": "concise", "new_value": "concise"},
            repetition_info={"is_repetition": True, "repetition_count": 2},
        )
        assert pc.drift_info["is_drift"] is True
        assert pc.preference_overrides["new_value"] == "concise"
        assert pc.repetition_info["repetition_count"] == 2


class TestP4SSEEventTypes:
    """P4 SSE 事件类型常量测试。"""

    def test_p4_event_types_exist(self):
        """所有 P4 SSE 事件类型已定义。"""
        assert SSEEventType.DRIFT_DETECTED == "drift_detected"
        assert SSEEventType.CONTRADICTION_DETECTED == "contradiction_detected"
        assert SSEEventType.RETRIEVAL_MISMATCH == "retrieval_mismatch"
        assert SSEEventType.PREFERENCE_CHANGED == "preference_changed"
        assert SSEEventType.REPETITION_DETECTED == "repetition_detected"


class TestP4ConfigItems:
    """P4 配置项测试。"""

    def test_p4_config_items_exist(self):
        """所有 P4 配置项已定义。"""
        from app.config import get_settings

        settings = get_settings()
        # P4-A
        assert hasattr(settings, "DRIFT_DETECTION_ENABLED")
        assert hasattr(settings, "DRIFT_SIMILARITY_THRESHOLD")
        # P4-B
        assert hasattr(settings, "CONTRADICTION_DETECTION_ENABLED")
        assert hasattr(settings, "CONTRADICTION_CHECK_USER_STATEMENTS")
        # P4-C
        assert hasattr(settings, "COREFERENCE_INJECT_HISTORY")
        assert hasattr(settings, "COREFERENCE_FOCUS_STACK_SIZE")
        # P4-D
        assert hasattr(settings, "RETRIEVAL_MATCH_CHECK_ENABLED")
        assert hasattr(settings, "RETRIEVAL_MATCH_THRESHOLD")
        # P4-F
        assert hasattr(settings, "PREFERENCE_DRIFT_ENABLED")
        # P4-G
        assert hasattr(settings, "REPETITION_DETECTION_ENABLED")
        assert hasattr(settings, "REPETITION_SIMILARITY_THRESHOLD")


class TestP4IntegrationFlow:
    """P4 集成流程测试 — 验证检测器间协作。"""

    @pytest.mark.asyncio
    async def test_drift_then_coreference_order(self):
        """漂移检测在指代消解前执行 — 漂移时重置焦点后再消解。"""
        from app.context.drift_detector import DriftDetector
        from app.context.focus_tracker import ConversationFocus, TopicTracker
        from app.context.coreference_resolver import CoreferenceResolver

        # 模拟：焦点是"天气"（北京），用户突然问"报销流程"
        tracker = TopicTracker(llm=None)
        tracker._push_focus(ConversationFocus(topic="天气", entity="北京", confidence=0.8))

        drift_detector = DriftDetector(embedder=None)
        query = "如何申请报销？"
        focus = tracker._last_focus

        # 漂移检测
        drift_result = await drift_detector.check(query, focus)
        assert drift_result.is_drift is True
        assert drift_result.action == "reset_focus"

        # 漂移 → 重置焦点
        tracker.reset_focus()
        assert tracker._last_focus is None

        # 重新提取焦点
        history = [
            {"role": "user", "content": "如何申请报销？"},
            {"role": "assistant", "content": "回复"},
        ]
        new_focus = await tracker.extract_focus(history)
        assert new_focus is not None
        assert "报销" in new_focus.topic

    @pytest.mark.asyncio
    async def test_preference_drift_does_not_block_other_detectors(self):
        """偏好偏移检测不阻塞其他检测器 — 纯规则零延迟。"""
        from app.context.preference_drift_detector import PreferenceDriftDetector
        from app.context.repetition_detector import RepetitionDetector

        pref_detector = PreferenceDriftDetector()
        rep_detector = RepetitionDetector(embedder=None)

        # 同时执行偏好检测和重复检测
        pref_result = pref_detector.detect("回答简单点")
        rep_result = await rep_detector.check("回答简单点", [])

        # 偏好检测成功
        assert pref_result.has_preference_change is True
        assert pref_result.new_value == "concise"

        # 重复检测（无 embedder → 非重复，不阻断）
        assert rep_result.is_repetition is False

    @pytest.mark.asyncio
    async def test_coreference_uses_focus_stack(self):
        """指代消解接收焦点栈参数 — P4-C 增强。"""
        from app.context.coreference_resolver import CoreferenceResolver
        from app.context.focus_tracker import ConversationFocus

        class CapturingLLM:
            def __init__(self):
                self.captured_prompt = ""

            async def chat(self, messages, stream=True, max_tokens=100):
                self.captured_prompt = messages[0].get("content", "")
                yield "上海限号政策"

        llm = CapturingLLM()
        resolver = CoreferenceResolver(llm=llm)
        focus = ConversationFocus(topic="限号政策", entity="北京")
        focus_stack = [
            ConversationFocus(topic="天气", entity="北京"),
            ConversationFocus(topic="限号政策", entity="北京"),
        ]
        history = [
            {"role": "user", "content": "北京今天限号多少？"},
            {"role": "assistant", "content": "3和7"},
        ]

        result = await resolver.resolve(
            "那上海呢？", focus,
            history=history,
            focus_stack=focus_stack,
        )

        assert result == "上海限号政策"
        # prompt 包含历史和焦点栈
        assert "北京今天限号" in llm.captured_prompt
        assert "天气" in llm.captured_prompt

    @pytest.mark.asyncio
    async def test_engine_receives_focus_and_drift(self):
        """引擎 answer() 接收 conversation_focus 和 drift_info — P4-E。"""
        from app.rag.engine import AgenticRAGEngine

        class CapturingLLM:
            def __init__(self):
                self.captured_dynamic = ""

            async def chat(self, messages, stream=False, **kwargs):
                # P0-1: 尾部约束提醒占用 [-1]，动态上下文在 [-2]
                self.captured_dynamic = messages[-2].get("content", "") if len(messages) >= 2 else ""
                yield "generate"

        class MockComp:
            async def search(self, *a, **kw):
                return []
            async def rerank(self, *a, **kw):
                return []
            async def call_tool(self, *a, **kw):
                return {}

        class MockGenerator:
            async def generate(self, *a, **kw):
                yield ""

        llm = CapturingLLM()
        engine = AgenticRAGEngine(
            llm=llm,
            mcp_client=MockComp(),
            retriever=MockComp(),
            reranker=MockComp(),
            generator=MockGenerator(),
        )

        focus = {"topic": "限号政策", "entity": "北京", "intent": "查询"}
        drift = {"is_drift": True, "drift_score": 0.8}

        # 消费引擎输出（只有 done 事件）
        chunks = []
        async for chunk in engine.answer(
            query="测试",
            user_id="u1",
            session_id="s1",
            conversation_focus=focus,
            drift_info=drift,
        ):
            chunks.append(chunk)

        # 验证 _think 的动态上下文包含焦点和漂移信息
        assert "限号政策" in llm.captured_dynamic
        assert "北京" in llm.captured_dynamic
        assert "切换了话题" in llm.captured_dynamic

    @pytest.mark.asyncio
    async def test_contradiction_background_task(self):
        """矛盾检测后台任务 — asyncio.create_task 不阻塞引擎流。"""
        from app.context.contradiction_detector import ContradictionDetector

        class SlowLLM:
            async def chat(self, messages, stream=False, max_tokens=150):
                await asyncio.sleep(0.1)  # 模拟 LLM 延迟
                yield '{"contradiction": false, "description": "", "severity": "low"}'

        detector = ContradictionDetector(llm=SlowLLM())
        history = [
            {"role": "user", "content": "北京今天限号3"},
            {"role": "assistant", "content": "是的"},
            {"role": "user", "content": "北京今天限号5"},
        ]

        # 后台启动
        task = asyncio.create_task(
            detector.check_user_contradiction("北京今天限号5", history)
        )

        # 立即返回，不等任务完成
        assert not task.done()

        # 等待完成
        result = await task
        assert task.done()
        assert result.contradiction_type == "user_statement"
