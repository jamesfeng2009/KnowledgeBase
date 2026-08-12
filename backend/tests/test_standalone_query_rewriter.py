"""StandaloneQueryRewriter 单元测试 — P1 多轮对话独立化改写器。

测试覆盖：
- 单轮对话直接返回消解结果（不调用 LLM 改写）
- 多轮对话 LLM 改写成功
- LLM 不可用降级
- 空查询处理
- _similarity / _sanitize 辅助方法
- _llm_extract_faq 多轮接入 rewriter（集成）
"""
from __future__ import annotations

import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ------------------------------------------------------------------
# Mock celery before importing app modules
# ------------------------------------------------------------------
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


def _make_mock_llm(response_text: str = "改写后的标准问题"):
    """创建 Mock LLM — chat 为异步生成器，yield 指定响应。"""
    llm = MagicMock()

    async def mock_chat(messages, tools=None, stream=False, **kwargs):
        yield response_text

    llm.chat = mock_chat
    return llm


def _make_mock_db():
    """创建 Mock AsyncSession。"""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.scalar = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ======================================================================
# StandaloneQueryRewriter 单元测试
# ======================================================================


class TestStandaloneQueryRewriter:
    """P1 多轮对话独立化改写器测试。"""

    @pytest.mark.asyncio
    async def test_single_turn_returns_resolved(self):
        """单轮对话（history < 2）直接返回消解结果，不调用 LLM 改写。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        llm = _make_mock_llm()
        rewriter = StandaloneQueryRewriter(llm)

        result = await rewriter.rewrite(
            current_query="公司差旅报销标准是什么？",
            history=[{"role": "user", "content": "公司差旅报销标准是什么？"}],
        )
        # 单轮对话无需独立化，返回原查询（消解器对完整句子不改写）
        assert result == "公司差旅报销标准是什么？"

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        """空查询直接返回空字符串。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        rewriter = StandaloneQueryRewriter(_make_mock_llm())
        result = await rewriter.rewrite(current_query="", history=[])
        assert result == ""

    @pytest.mark.asyncio
    async def test_multi_turn_llm_unavailable_degraded(self):
        """LLM 不可用时降级返回消解结果（不阻断流程）。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        # llm=None → rewriter 降级返回 resolved
        rewriter = StandaloneQueryRewriter(None)

        history = [
            {"role": "user", "content": "公司差旅报销标准？"},
            {"role": "assistant", "content": "经济舱按实报销..."},
            {"role": "user", "content": "那国际航班呢？"},
        ]
        result = await rewriter.rewrite(
            current_query="那国际航班呢？",
            history=history,
        )
        # LLM 不可用时返回字符串（消解结果或原查询）
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_rewrite_with_mocked_internals(self):
        """多轮对话 LLM 改写成功（patch 内部方法隔离 LLM 调用）。"""
        from app.context.focus_tracker import ConversationFocus
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        llm = _make_mock_llm("公司差旅管理办法中国际航班报销标准是什么")
        rewriter = StandaloneQueryRewriter(llm)

        focus = ConversationFocus(
            topic="差旅报销", entity="公司", intent="查询"
        )
        with patch.object(
            rewriter._tracker, "extract_focus", new=AsyncMock(return_value=focus)
        ), patch.object(
            rewriter._resolver, "resolve",
            new=AsyncMock(return_value="国际航班报销标准"),
        ):
            result = await rewriter.rewrite(
                current_query="那国际航班呢？",
                history=[
                    {"role": "user", "content": "公司差旅报销标准？"},
                    {"role": "assistant", "content": "经济舱按实报销..."},
                ],
            )
        # LLM 返回改写后的 Q（经 _sanitize 清理）
        assert "国际航班" in result
        assert "差旅" in result or "报销" in result

    @pytest.mark.asyncio
    async def test_rewrite_too_short_degraded(self):
        """改写后 Q 太短（< 5 字）退化返回消解结果。"""
        from app.context.focus_tracker import ConversationFocus
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        # LLM 返回过短文本
        llm = _make_mock_llm("ab")
        rewriter = StandaloneQueryRewriter(llm)

        focus = ConversationFocus(topic="差旅", entity="公司")
        with patch.object(
            rewriter._tracker, "extract_focus", new=AsyncMock(return_value=focus)
        ), patch.object(
            rewriter._resolver, "resolve",
            new=AsyncMock(return_value="国际航班报销标准"),
        ):
            result = await rewriter.rewrite(
                current_query="那国际航班呢？",
                history=[
                    {"role": "user", "content": "..."},
                    {"role": "assistant", "content": "..."},
                ],
            )
        # 改写后太短 → 退化返回 resolved
        assert result == "国际航班报销标准"

    @pytest.mark.asyncio
    async def test_rewrite_no_change_needed(self):
        """改写后 Q 与消解结果几乎相同 → 直接用消解结果。"""
        from app.context.focus_tracker import ConversationFocus
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        # LLM 返回与 resolved 完全相同
        resolved_text = "国际航班报销标准是什么"
        llm = _make_mock_llm(resolved_text)
        rewriter = StandaloneQueryRewriter(llm)

        focus = ConversationFocus(topic="差旅", entity="公司")
        with patch.object(
            rewriter._tracker, "extract_focus", new=AsyncMock(return_value=focus)
        ), patch.object(
            rewriter._resolver, "resolve",
            new=AsyncMock(return_value=resolved_text),
        ):
            result = await rewriter.rewrite(
                current_query="那国际航班呢？",
                history=[
                    {"role": "user", "content": "..."},
                    {"role": "assistant", "content": "..."},
                ],
            )
        # 相似度 > 0.95 → 返回 resolved
        assert result == resolved_text

    def test_similarity_identical(self):
        """相同字符串相似度为 1.0。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        assert StandaloneQueryRewriter._similarity("测试", "测试") == 1.0

    def test_similarity_empty(self):
        """空字符串相似度为 0.0。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        assert StandaloneQueryRewriter._similarity("", "测试") == 0.0

    def test_similarity_partial(self):
        """部分相似字符串相似度在 (0, 1)。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        sim = StandaloneQueryRewriter._similarity("abc", "abd")
        assert 0 < sim < 1.0

    def test_sanitize_strips_quotes(self):
        """_sanitize 去除包裹引号。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        assert StandaloneQueryRewriter._sanitize('"测试"') == "测试"
        assert StandaloneQueryRewriter._sanitize("「测试」") == "测试"
        assert StandaloneQueryRewriter._sanitize('"测试"') == "测试"

    def test_sanitize_collapses_whitespace(self):
        """_sanitize 折叠换行为空格。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        assert StandaloneQueryRewriter._sanitize("测\n试") == "测 试"

    def test_sanitize_empty(self):
        """_sanitize 空输入返回空。"""
        from app.context.standalone_query_rewriter import StandaloneQueryRewriter

        assert StandaloneQueryRewriter._sanitize("") == ""
        assert StandaloneQueryRewriter._sanitize(None) == ""


# ======================================================================
# _llm_extract_faq 多轮接入 rewriter 集成测试
# ======================================================================


class TestFaqExtractionWithRewriter:
    """P1 _llm_extract_faq 多轮接入 StandaloneQueryRewriter 测试。"""

    @pytest.mark.asyncio
    async def test_llm_extract_faq_with_history_triggers_rewriter(self):
        """多轮对话（history >= 2）触发 StandaloneQueryRewriter 改写。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        llm = _make_mock_llm('{"question":"独立问题","answer":"答案","tags":[],"confidence":0.8}')
        service = KnowledgeCompoundingService(llm, db)

        context = {
            "user_query": "那国际航班呢？",
            "assistant_answer": "国际航班商务舱...",
            "feedback_content": "好评",
            "history": [
                {"role": "user", "content": "公司差旅报销标准？"},
                {"role": "assistant", "content": "经济舱按实报销..."},
            ],
        }

        # patch StandaloneQueryRewriter.rewrite 验证被调用
        with patch(
            "app.context.standalone_query_rewriter.StandaloneQueryRewriter.rewrite",
            new=AsyncMock(return_value="公司差旅管理办法中国际航班报销标准"),
        ) as mock_rewrite:
            result = await service._llm_extract_faq(context)

        # rewriter 被调用
        mock_rewrite.assert_called_once()
        assert result["question"] == "独立问题"

    @pytest.mark.asyncio
    async def test_llm_extract_faq_single_turn_skips_rewriter(self):
        """单轮对话（history < 2）跳过 StandaloneQueryRewriter。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        llm = _make_mock_llm('{"question":"问题","answer":"答案","tags":[],"confidence":0.8}')
        service = KnowledgeCompoundingService(llm, db)

        context = {
            "user_query": "单轮问题",
            "assistant_answer": "单轮答案",
            "feedback_content": "",
            "history": [{"role": "user", "content": "单轮问题"}],  # < 2
        }

        with patch(
            "app.context.standalone_query_rewriter.StandaloneQueryRewriter.rewrite",
            new=AsyncMock(return_value="不应被调用"),
        ) as mock_rewrite:
            result = await service._llm_extract_faq(context)

        # rewriter 未被调用
        mock_rewrite.assert_not_called()
        assert result["question"] == "问题"

    @pytest.mark.asyncio
    async def test_llm_extract_faq_rewriter_failure_degraded(self):
        """rewriter 失败时降级（不影响后续 LLM 提取）。"""
        from app.services.knowledge_compounding import KnowledgeCompoundingService

        db = _make_mock_db()
        llm = _make_mock_llm('{"question":"问题","answer":"答案","tags":[],"confidence":0.8}')
        service = KnowledgeCompoundingService(llm, db)

        context = {
            "user_query": "那国际航班呢？",
            "assistant_answer": "国际航班商务舱...",
            "feedback_content": "",
            "history": [
                {"role": "user", "content": "..."},
                {"role": "assistant", "content": "..."},
            ],
        }

        # rewriter 抛异常 → 降级用原 user_query
        with patch(
            "app.context.standalone_query_rewriter.StandaloneQueryRewriter.rewrite",
            new=AsyncMock(side_effect=RuntimeError("rewriter failed")),
        ):
            result = await service._llm_extract_faq(context)

        # 降级后仍正常返回（LLM 提取成功）
        assert result["question"] == "问题"
        assert result["answer"] == "答案"
