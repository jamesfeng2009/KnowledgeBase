"""
P3-F LLM 事实提取单元测试。

覆盖：
    - LLM 提取成功（mock）
    - LLM 返回 NONE
    - LLM 异常降级为关键词启发式
    - 对话太短不提取
    - 配置开关关闭
    - 关键词启发式提取
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.memory_manager import MemoryManager


@pytest.fixture
def memory_manager():
    """创建 MemoryManager with mock 依赖。"""
    mock_db = MagicMock()
    mgr = MemoryManager(mock_db)
    # 替换子管理器为 mock
    mgr.mem0 = AsyncMock()
    mgr.checkpoint = AsyncMock()
    mgr.graphiti = MagicMock()
    return mgr


class TestLLMFactExtraction:
    """LLM 事实提取测试。"""

    @pytest.mark.asyncio
    async def test_llm_extract_success(self, memory_manager):
        """LLM 成功提取偏好。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=200):
                yield "preference|用户偏好中文回复\nfact|用户所在部门是技术部"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "我喜欢用中文回复，我是技术部的，平时主要做后端开发工作"},
                    {"role": "assistant", "content": "好的，我会用中文回复您。请问有什么可以帮您的？"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        assert len(result) == 2
        assert "中文" in result[0]
        assert "技术部" in result[1]
        # 验证 add_fact 被调用
        assert memory_manager.mem0.add_fact.call_count == 2

    @pytest.mark.asyncio
    async def test_llm_extract_none(self, memory_manager):
        """LLM 返回 NONE → 无事实提取。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=200):
                yield "NONE"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "你好，我想了解一下公司的报销流程具体是怎样的"},
                    {"role": "assistant", "content": "您好，公司的报销流程主要包括提交申请、审批和打款三个步骤"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        assert result == []
        assert memory_manager.mem0.add_fact.call_count == 0

    @pytest.mark.asyncio
    async def test_llm_exception_fallback(self, memory_manager):
        """LLM 异常 → 降级为关键词启发式。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=200):
                raise RuntimeError("API error")
                yield  # make it an async generator

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "我喜欢简洁的回复，不要太长，直接说重点就行"},
                    {"role": "assistant", "content": "好的，我明白了，以后会尽量简洁地回复您。"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        # 降级为关键词提取 — 应该提取到"我喜欢"
        assert len(result) == 1
        assert "我喜欢" in result[0]

    @pytest.mark.asyncio
    async def test_short_conversation_no_extraction(self, memory_manager):
        """对话太短不提取。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=200):
                yield "preference|test"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "hi"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        # 对话太短（< 50 字符），不提取
        assert result == []

    @pytest.mark.asyncio
    async def test_config_disabled_fallback(self, memory_manager):
        """配置关闭 LLM 提取 → 直接走关键词启发式。"""
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            messages = [
                {"role": "user", "content": "我喜欢详细的回复"},
                {"role": "assistant", "content": "好的"},
            ]
            result = await memory_manager.extract_and_save_facts(
                uuid.uuid4(), messages
            )

        # 直接走关键词提取
        assert len(result) == 1
        assert "我喜欢" in result[0]


class TestKeywordFactExtraction:
    """关键词启发式事实提取测试。"""

    @pytest.mark.asyncio
    async def test_keyword_extract_preference(self, memory_manager):
        """关键词提取 — 偏好检测。"""
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            messages = [
                {"role": "user", "content": "请使用中文回复"},
            ]
            result = await memory_manager.extract_and_save_facts(
                uuid.uuid4(), messages
            )

        assert len(result) == 1
        assert "中文" in result[0]

    @pytest.mark.asyncio
    async def test_keyword_extract_no_match(self, memory_manager):
        """关键词提取 — 无匹配。"""
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            messages = [
                {"role": "user", "content": "今天天气怎么样"},
            ]
            result = await memory_manager.extract_and_save_facts(
                uuid.uuid4(), messages
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_keyword_extract_multiple_keywords(self, memory_manager):
        """关键词提取 — 每条消息只匹配第一个关键词。"""
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = False
            messages = [
                {"role": "user", "content": "我喜欢简洁回复，我偏好列表格式"},
            ]
            result = await memory_manager.extract_and_save_facts(
                uuid.uuid4(), messages
            )

        # 每条消息只匹配第一个关键词（"我喜欢"）
        assert len(result) == 1
        assert "我喜欢" in result[0]
