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
    mgr.mem0.search_facts.return_value = []  # 默认无重复
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


class TestImportanceScoring:
    """P1-1: 重要性评分过滤测试。"""

    @pytest.mark.asyncio
    async def test_low_importance_filtered(self, memory_manager):
        """低重要性事实被过滤，不入库。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "preference|1|用户随便提了一下天气\nfact|2|不太重要的细节"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "今天天气不错，随便聊聊，另外我对后端开发比较感兴趣"},
                    {"role": "assistant", "content": "是的，天气很好。您对后端开发感兴趣可以了解一下微服务架构。"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        # 两条都是低重要性（< 3），全部过滤
        assert result == []
        assert memory_manager.mem0.add_fact.call_count == 0

    @pytest.mark.asyncio
    async def test_high_importance_kept(self, memory_manager):
        """高重要性事实保留。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "preference|4|用户偏好简洁回答\nfact|5|项目截止日期是下周五"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "请记住我们的项目截止日期是下周五，另外以后回答简洁一点"},
                    {"role": "assistant", "content": "好的，已记住项目截止日期是下周五，以后回答会简洁一些。"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        assert len(result) == 2
        assert memory_manager.mem0.add_fact.call_count == 2

    @pytest.mark.asyncio
    async def test_mixed_importance(self, memory_manager):
        """混合重要性：仅保留 >= 3 的事实。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "preference|1|不太重要的偏好\nfact|4|重要的事实信息\npreference|2|低价值偏好"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "我想了解一下公司的报销流程和年假政策具体规定是什么"},
                    {"role": "assistant", "content": "报销流程是先提交申请然后审批，年假是每年15天。"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        # 仅 fact|4 保留
        assert len(result) == 1
        assert "重要" in result[0]

    @pytest.mark.asyncio
    async def test_backward_compatible_no_importance(self, memory_manager):
        """旧格式 category|content 无 importance 字段，默认 importance=3 保留。"""
        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "preference|用户偏好中文回复"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "我喜欢用中文回复，这样看起来更自然更舒服一些"},
                    {"role": "assistant", "content": "好的，我会用中文回复您。请问有什么可以帮您的？"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        # 旧格式默认 importance=3 >= 3，保留
        assert len(result) == 1
        assert memory_manager.mem0.add_fact.call_count == 1


class TestDeduplication:
    """P1-1: 语义去重测试。"""

    @pytest.mark.asyncio
    async def test_duplicate_fact_skipped(self, memory_manager):
        """语义相似的已有事实 → 跳过不写入。"""
        # 模拟已有相似事实
        existing_fact = MagicMock()
        existing_fact.fact_text = "用户偏好简洁回答"
        memory_manager.mem0.search_facts.return_value = [existing_fact]

        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "preference|4|用户喜欢简洁的回复方式"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "请记住以后回答简洁一点，不要太啰嗦了"},
                    {"role": "assistant", "content": "好的，我会尽量简洁地回答您的问题。"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        # 重复 → 不写入
        assert result == []
        assert memory_manager.mem0.add_fact.call_count == 0

    @pytest.mark.asyncio
    async def test_no_duplicate_fact_saved(self, memory_manager):
        """无重复 → 正常写入。"""
        # 默认 search_facts 返回空（无重复）
        memory_manager.mem0.search_facts.return_value = []

        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "preference|4|用户偏好详细回答"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "以后请详细展开回答，我想了解更多的背景信息和细节"},
                    {"role": "assistant", "content": "好的，我会详细展开回答您的问题。"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        assert len(result) == 1
        assert memory_manager.mem0.add_fact.call_count == 1

    @pytest.mark.asyncio
    async def test_dedup_check_failure_does_not_block(self, memory_manager):
        """去重检查异常 → 不阻塞写入（避免漏记）。"""
        memory_manager.mem0.search_facts.side_effect = RuntimeError("search failed")

        class MockLLM:
            async def chat(self, messages, stream=True, max_tokens=300):
                yield "preference|4|用户偏好中文"

        with patch("app.memory.memory_manager.get_llm_provider", return_value=MockLLM()):
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value.LLM_FACT_EXTRACTION_ENABLED = True
                messages = [
                    {"role": "user", "content": "我喜欢中文回复，这样看起来更亲切更自然一些"},
                    {"role": "assistant", "content": "好的，我会用中文回复您的问题。"},
                ]
                result = await memory_manager.extract_and_save_facts(
                    uuid.uuid4(), messages
                )

        # 去重检查失败 → 不阻塞写入
        assert len(result) == 1
        assert memory_manager.mem0.add_fact.call_count == 1
