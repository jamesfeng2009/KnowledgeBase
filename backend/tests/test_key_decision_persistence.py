"""
跨轮关键决策持久化测试 — app/memory/memory_manager.py。

覆盖范围：
    - extract_and_save_key_decisions 方法
    - 启发式判断是否包含关键决策
    - LLM 提取关键决策
    - 关键决策持久化到 working memory（24h 过期）
    - 无关键决策时跳过
    - LLM 不可用时优雅降级
"""

from __future__ import annotations

import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


class TestExtractAndSaveKeyDecisions:
    """extract_and_save_key_decisions 方法测试。"""

    def _make_memory_manager(self) -> MagicMock:
        """创建模拟 MemoryManager。"""
        manager = MagicMock()
        manager.mem0 = MagicMock()
        manager.mem0.add_fact = AsyncMock()
        return manager

    @pytest.mark.asyncio
    async def test_no_decision_keywords_skip(self) -> None:
        """无决策关键词时跳过提取。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_memory_manager()
        # 绑定真实方法
        result = await MemoryManager.extract_and_save_key_decisions(
            manager,
            user_id=uuid.uuid4(),
            query="今天天气怎么样",
            answer="今天北京晴朗，气温25度。",
        )
        assert result is None
        manager.mem0.add_fact.assert_not_called()

    @pytest.mark.asyncio
    async def test_decision_keyword_in_query_triggers(self) -> None:
        """用户查询中包含决策关键词时触发提取。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_memory_manager()

        mock_llm = MagicMock()
        async def mock_chat(*args, **kwargs):
            yield "用户确认选择方案A"

        mock_llm.chat = mock_chat

        with patch("app.memory.memory_manager.get_llm_provider", return_value=mock_llm):
            result = await MemoryManager.extract_and_save_key_decisions(
                manager,
                user_id=uuid.uuid4(),
                query="我选择方案A",
                answer="已为您选择方案A。",
            )

        assert result == "用户确认选择方案A"
        manager.mem0.add_fact.assert_called_once()
        call_kwargs = manager.mem0.add_fact.call_args.kwargs
        assert call_kwargs["category"] == "working"
        assert call_kwargs["ttl_hours"] == 24
        assert "用户确认选择方案A" in call_kwargs["fact_text"]

    @pytest.mark.asyncio
    async def test_decision_keyword_in_answer_triggers(self) -> None:
        """AI 回答中包含确认关键词时触发提取。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_memory_manager()

        mock_llm = MagicMock()
        async def mock_chat(*args, **kwargs):
            yield "确认报销金额5000元"

        mock_llm.chat = mock_chat

        with patch("app.memory.memory_manager.get_llm_provider", return_value=mock_llm):
            result = await MemoryManager.extract_and_save_key_decisions(
                manager,
                user_id=uuid.uuid4(),
                query="报销金额是多少",
                answer="确认报销金额5000元。",
            )

        assert result == "确认报销金额5000元"
        manager.mem0.add_fact.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_returns_none_skip(self) -> None:
        """LLM 返回 NONE 时不保存。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_memory_manager()

        mock_llm = MagicMock()
        async def mock_chat(*args, **kwargs):
            yield "NONE"

        mock_llm.chat = mock_chat

        with patch("app.memory.memory_manager.get_llm_provider", return_value=mock_llm):
            result = await MemoryManager.extract_and_save_key_decisions(
                manager,
                user_id=uuid.uuid4(),
                query="我确认这个决定",
                answer="好的。",
            )

        assert result is None
        manager.mem0.add_fact.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_unavailable_graceful_degradation(self) -> None:
        """LLM 不可用时优雅降级返回 None。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_memory_manager()

        with patch("app.memory.memory_manager.get_llm_provider", side_effect=Exception("no llm")):
            result = await MemoryManager.extract_and_save_key_decisions(
                manager,
                user_id=uuid.uuid4(),
                query="我选择方案B",
                answer="已选择方案B。",
            )

        assert result is None
        manager.mem0.add_fact.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_chat_exception_degradation(self) -> None:
        """LLM chat 异常时优雅降级。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_memory_manager()

        mock_llm = MagicMock()
        async def mock_chat(*args, **kwargs):
            raise Exception("llm error")
            yield  # never reached

        mock_llm.chat = mock_chat

        with patch("app.memory.memory_manager.get_llm_provider", return_value=mock_llm):
            result = await MemoryManager.extract_and_save_key_decisions(
                manager,
                user_id=uuid.uuid4(),
                query="我选择方案C",
                answer="已选择。",
            )

        assert result is None
        manager.mem0.add_fact.assert_not_called()

    @pytest.mark.asyncio
    async def test_decision_persisted_with_correct_ttl(self) -> None:
        """关键决策以 24h TTL 持久化到 working memory。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_memory_manager()

        mock_llm = MagicMock()
        async def mock_chat(*args, **kwargs):
            yield "设定审批阈值为10000元"

        mock_llm.chat = mock_chat

        with patch("app.memory.memory_manager.get_llm_provider", return_value=mock_llm):
            await MemoryManager.extract_and_save_key_decisions(
                manager,
                user_id=uuid.uuid4(),
                query="设定审批阈值",
                answer="已设定审批阈值为10000元。",
            )

        call_kwargs = manager.mem0.add_fact.call_args.kwargs
        assert call_kwargs["ttl_hours"] == 24
        assert call_kwargs["category"] == "working"
        assert "关键决策" in call_kwargs["fact_text"]
