"""
P0-1 记忆事实溯源测试 — app/memory/memory_manager.py。

覆盖：
    - LLM 路径：绑定到最近一条用户消息（source_type/source_ref_id/raw_excerpt）
    - 关键词路径：按消息精确绑定对应 id 与摘录
    - 缺 message_ids（None / 长度不足）→ source 落 None，不阻断
    - 多消息对齐不错位
    - message_ids 缺省时向后兼容（不抛异常）
"""

from __future__ import annotations

import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.memory.memory_manager import MemoryManager


@pytest.fixture
def memory_manager():
    """MemoryManager with mock 依赖（对齐 test_fact_extraction 约定）。"""
    mgr = MemoryManager(MagicMock())
    mgr.mem0 = AsyncMock()
    mgr.checkpoint = AsyncMock()
    mgr.graphiti = MagicMock()
    return mgr


class TestLLMSourceBinding:
    """LLM 提取路径 — 事实绑定到最近一条用户消息。"""

    @pytest.mark.asyncio
    async def test_llm_fact_binds_to_last_message(self, memory_manager):
        """LLM 提取成功时，来源绑定到 messages 最后一条的 id 与摘录。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=200):
                yield "preference|用户偏好使用Python"

        captured = {}

        async def fake_consolidated(user_id, fact_text, category, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        memory_manager._consolidated_add = fake_consolidated

        message_id = uuid.uuid4()
        messages = [
            {"role": "user", "content": "我们简单聊一下"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "我喜欢使用Python编程，请记住"},
        ]
        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages, [uuid.uuid4(), uuid.uuid4(), message_id]
                )

        assert captured["source_type"] == "message"
        assert captured["source_ref_id"] == message_id
        assert "我喜欢使用Python编程" in captured["raw_excerpt"]

    @pytest.mark.asyncio
    async def test_llm_no_message_ids_degrades(self, memory_manager):
        """LLM 路径缺 message_ids → 来源落 None，不阻断提取。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=200):
                yield "preference|用户喜欢Python"

        captured = {}

        async def fake_consolidated(user_id, fact_text, category, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        memory_manager._consolidated_add = fake_consolidated

        messages = [{"role": "user", "content": "我喜欢使用Python编程，这是我长期的习惯，希望以后一直用这个语言来写代码，真的是一个非常稳定的选择"}]
        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                await memory_manager.extract_and_save_facts(uuid.uuid4(), messages)

        assert captured["source_type"] == "message"
        assert captured["source_ref_id"] is None
        assert captured["raw_excerpt"] is not None


class TestKeywordSourceBinding:
    """关键词提取路径 — 按消息精确绑定来源。"""

    @pytest.mark.asyncio
    async def test_keyword_binds_to_correct_message(self, memory_manager):
        """多消息时，每个偏好事实绑定到各自消息的 id。"""
        captured = []

        async def fake_consolidated(user_id, fact_text, category, **kwargs):
            captured.append((fact_text, kwargs))
            return MagicMock()

        memory_manager._consolidated_add = fake_consolidated

        id0, id1 = uuid.uuid4(), uuid.uuid4()
        messages = [
            {"role": "user", "content": "我喜欢使用Python"},
            {"role": "user", "content": "我偏好深夜工作"},
        ]
        # 走关键词路径（LLM 开关关闭）
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            await memory_manager.extract_and_save_facts(
                uuid.uuid4(), messages, [id0, id1]
            )

        assert len(captured) == 2
        # 第一条消息 → id0
        assert captured[0][1]["source_type"] == "message"
        assert captured[0][1]["source_ref_id"] == id0
        # 第二条消息 → id1
        assert captured[1][1]["source_ref_id"] == id1

    @pytest.mark.asyncio
    async def test_keyword_excerpt_is_per_message(self, memory_manager):
        """raw_excerpt 应来自该条消息内容，而非混用。"""
        captured = {}

        async def fake_consolidated(user_id, fact_text, category, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        memory_manager._consolidated_add = fake_consolidated

        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            await memory_manager.extract_and_save_facts(
                uuid.uuid4(),
                [{"role": "user", "content": "我偏好使用Go"}],
                [uuid.uuid4()],
            )

        assert "我偏好使用Go" in captured["raw_excerpt"]

    @pytest.mark.asyncio
    async def test_keyword_short_message_ids_degrades(self, memory_manager):
        """message_ids 长度不足 → 越界消息来源落 None，不抛异常。"""
        captured = {}

        async def fake_consolidated(user_id, fact_text, category, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        memory_manager._consolidated_add = fake_consolidated

        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            # 只有 1 个 id，但 2 条命中消息 → 第二条 source_ref_id=None
            await memory_manager.extract_and_save_facts(
                uuid.uuid4(),
                [{"role": "user", "content": "我偏好打字"}, {"role": "user", "content": "我偏好阅读"}],
                [uuid.uuid4()],
            )

        assert captured["source_ref_id"] is None

    @pytest.mark.asyncio
    async def test_backward_compatible_no_message_ids(self, memory_manager):
        """完全不传 message_ids（旧调用）→ 不抛异常且 source 落 None。"""
        captured = {}

        async def fake_consolidated(user_id, fact_text, category, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        memory_manager._consolidated_add = fake_consolidated

        # 强制走关键词路径，避免受 LLM_FACT_EXTRACTION_ENABLED 默认值影响
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            result = await memory_manager.extract_and_save_facts(
                uuid.uuid4(),
                [{"role": "user", "content": "我偏好早起"}],
            )

        assert len(result) == 1  # 关键词命中"我偏好早起" → 提取 1 条
        # keyword 路径确实触发了 consolidated（说明无 message_ids 也能跑通）
        assert captured["source_ref_id"] is None


class TestConsolidatedForwardsSource:
    """_consolidated_add 将溯源参数转发给 mem0.add_fact。"""

    @pytest.mark.asyncio
    async def test_consolidated_forwards_to_add_fact(self, memory_manager):
        """经整合路径落盘时，溯源参数传到 mem0.add_fact。"""
        memory_manager.mem0.add_fact = AsyncMock()
        memory_manager.arbiter = MagicMock()
        memory_manager.arbiter.consolidate = AsyncMock()
        memory_manager.arbiter.consolidate.return_value.action = "keep"
        memory_manager.arbiter.consolidate.return_value.superseded_ids = []

        sid = uuid.uuid4()
        await memory_manager._consolidated_add(
            uuid.uuid4(), "用户喜欢Python", "preference",
            source_type="message", source_ref_id=sid, raw_excerpt="我喜欢Python",
        )

        memory_manager.mem0.add_fact.assert_awaited_once()
        kwargs = memory_manager.mem0.add_fact.await_args.kwargs
        assert kwargs["source_type"] == "message"
        assert kwargs["source_ref_id"] == sid
        assert kwargs["raw_excerpt"] == "我喜欢Python"