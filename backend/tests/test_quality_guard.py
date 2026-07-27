"""
RAG 质量守卫测试 — app/rag/quality_guard.py。

覆盖范围：
    - RetrievalQualityResult 数据类
    - QualityGuard.check_retrieval_quality（检索质量检查）
    - QualityGuard.should_retry_retrieval（重试决策）
    - QualityGuard.get_expanded_top_k（扩展 top_k）
    - QualityGuard.check_generation_quality（生成质量检查）
    - QualityGuard.is_low_confidence（低置信度判断）
    - config 配置项
    - engine 集成：_retrieve 质量守卫 + _reflect 升级
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery（测试环境未安装）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# RetrievalQualityResult 数据类测试
# ======================================================================


class TestRetrievalQualityResult:
    """RetrievalQualityResult 数据类测试。"""

    def test_creation(self) -> None:
        from app.rag.quality_guard import RetrievalQualityResult

        result = RetrievalQualityResult(
            mean_score=0.5, passed=True, doc_count=5
        )
        assert result.mean_score == 0.5
        assert result.passed is True
        assert result.doc_count == 5
        assert result.retry_attempted is False

    def test_defaults(self) -> None:
        from app.rag.quality_guard import RetrievalQualityResult

        result = RetrievalQualityResult(mean_score=0.1, passed=False)
        assert result.retry_attempted is False
        assert result.doc_count == 0


# ======================================================================
# 检索质量守卫测试
# ======================================================================


class TestCheckRetrievalQuality:
    """check_retrieval_quality 方法测试。"""

    def test_empty_docs(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        result = guard.check_retrieval_quality([])
        assert result.mean_score == 0.0
        assert result.passed is False
        assert result.doc_count == 0

    def test_high_scores_pass(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [
            {"content": "doc1", "score": 0.8},
            {"content": "doc2", "score": 0.9},
        ]
        result = guard.check_retrieval_quality(docs)
        assert result.mean_score == 0.85
        assert result.passed is True
        assert result.doc_count == 2

    def test_low_scores_fail(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [
            {"content": "doc1", "score": 0.1},
            {"content": "doc2", "score": 0.2},
        ]
        result = guard.check_retrieval_quality(docs)
        assert result.mean_score == 0.15
        assert result.passed is False
        assert result.doc_count == 2

    def test_boundary_threshold(self) -> None:
        """均值正好等于阈值时 passed=True。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [
            {"content": "doc1", "score": 0.3},
            {"content": "doc2", "score": 0.3},
        ]
        result = guard.check_retrieval_quality(docs)
        assert result.mean_score == 0.3
        assert result.passed is True  # >= threshold

    def test_no_score_field(self) -> None:
        """文档无 score 字段时返回 0 分。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [{"content": "doc1"}, {"content": "doc2"}]
        result = guard.check_retrieval_quality(docs)
        assert result.mean_score == 0.0
        assert result.passed is False

    def test_none_score_skipped(self) -> None:
        """score 为 None 的文档被跳过。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [
            {"content": "doc1", "score": None},
            {"content": "doc2", "score": 0.6},
        ]
        result = guard.check_retrieval_quality(docs)
        assert result.mean_score == 0.6
        assert result.passed is True


class TestShouldRetryRetrieval:
    """should_retry_retrieval 方法测试。"""

    def test_should_retry_low_score(self) -> None:
        from app.rag.quality_guard import QualityGuard, RetrievalQualityResult

        guard = QualityGuard()
        result = RetrievalQualityResult(
            mean_score=0.1, passed=False, doc_count=5
        )
        assert guard.should_retry_retrieval(result, 0) is True

    def test_should_not_retry_high_score(self) -> None:
        from app.rag.quality_guard import QualityGuard, RetrievalQualityResult

        guard = QualityGuard()
        result = RetrievalQualityResult(
            mean_score=0.8, passed=True, doc_count=5
        )
        assert guard.should_retry_retrieval(result, 0) is False

    def test_should_not_retry_max_reached(self) -> None:
        from app.rag.quality_guard import QualityGuard, RetrievalQualityResult

        guard = QualityGuard()
        result = RetrievalQualityResult(
            mean_score=0.1, passed=False, doc_count=5
        )
        # retry_count >= max_retries(1)
        assert guard.should_retry_retrieval(result, 1) is False

    def test_should_not_retry_empty_docs(self) -> None:
        from app.rag.quality_guard import QualityGuard, RetrievalQualityResult

        guard = QualityGuard()
        result = RetrievalQualityResult(
            mean_score=0.0, passed=False, doc_count=0
        )
        assert guard.should_retry_retrieval(result, 0) is False

    def test_should_not_retry_when_disabled(self) -> None:
        from app.rag.quality_guard import QualityGuard, RetrievalQualityResult

        guard = QualityGuard()
        with patch("app.rag.quality_guard.get_settings") as mock_settings:
            mock_settings.return_value.RAG_QUALITY_GUARD_ENABLED = False
            mock_settings.return_value.RAG_RETRIEVAL_MAX_RETRIES = 1
            mock_settings.return_value.RAG_RETRIEVAL_SCORE_THRESHOLD = 0.3
            result = RetrievalQualityResult(
                mean_score=0.1, passed=False, doc_count=5
            )
            assert guard.should_retry_retrieval(result, 0) is False


class TestGetExpandedTopK:
    """get_expanded_top_k 方法测试。"""

    def test_default_values(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        expanded = guard.get_expanded_top_k()
        # RAG_RERANK_TOP_K(5) + RAG_RETRIEVAL_EXPAND_TOP_K(10) = 15
        assert expanded == 15


# ======================================================================
# 生成质量守卫测试
# ======================================================================


class TestCheckGenerationQuality:
    """check_generation_quality 方法测试。"""

    @pytest.mark.asyncio
    async def test_successful_evaluation(self) -> None:
        """成功调用 LLMJudgeService 返回 EvalResult。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()

        mock_eval_result = MagicMock()
        mock_eval_result.citation_accuracy = 4
        mock_eval_result.completeness = 5
        mock_eval_result.hallucination_inverse = 4
        mock_eval_result.total_score = 4.33
        mock_eval_result.passed = True
        mock_eval_result.error = None

        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(return_value=mock_eval_result)

        guard._judge_service = mock_judge

        result = await guard.check_generation_quality(
            query="测试问题", answer="测试答案", contexts=["文档1"]
        )

        assert result is not None
        assert result.citation_accuracy == 4
        assert result.completeness == 5
        mock_judge.evaluate_single.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_answer_returns_none(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        result = await guard.check_generation_quality(
            query="test", answer="", contexts=[]
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_judge_not_available(self) -> None:
        """LLMJudgeService 不可用时返回 None。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        guard._judge_service = None
        # 模拟 _get_judge_service 返回 None
        with patch.object(guard, "_get_judge_service", return_value=None):
            result = await guard.check_generation_quality(
                query="test", answer="answer", contexts=["ctx"]
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_judge_exception_handled(self) -> None:
        """Judge 异常时返回 None，不抛出。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(side_effect=Exception("Judge error"))
        guard._judge_service = mock_judge

        result = await guard.check_generation_quality(
            query="test", answer="answer", contexts=["ctx"]
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        with patch("app.rag.quality_guard.get_settings") as mock_settings:
            mock_settings.return_value.RAG_QUALITY_GUARD_ENABLED = False
            mock_settings.return_value.RAG_FAITHFULNESS_THRESHOLD = 3.0
            result = await guard.check_generation_quality(
                query="test", answer="answer", contexts=["ctx"]
            )
            assert result is None
            # 守卫关闭时不应调用 Judge
            assert guard._judge_service is None


class TestIsLowConfidence:
    """is_low_confidence 方法测试。"""

    def test_low_faithfulness(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        mock_result = MagicMock()
        mock_result.hallucination_inverse = 2  # < 3.0 threshold
        mock_result.error = None

        assert guard.is_low_confidence(mock_result) is True

    def test_high_faithfulness(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        mock_result = MagicMock()
        mock_result.hallucination_inverse = 4  # >= 3.0 threshold
        mock_result.error = None

        assert guard.is_low_confidence(mock_result) is False

    def test_none_result(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        assert guard.is_low_confidence(None) is False

    def test_error_result(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        mock_result = MagicMock()
        mock_result.hallucination_inverse = 2
        mock_result.error = "some error"

        assert guard.is_low_confidence(mock_result) is False


# ======================================================================
# 配置项测试
# ======================================================================


class TestQualityGuardConfig:
    """RAG 质量守卫配置项测试。"""

    def test_retrieval_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert settings.RAG_RETRIEVE_TOP_K == 20
        assert settings.RAG_RERANK_TOP_K == 5
        assert settings.RAG_MAX_ITERATIONS == 5

    def test_guard_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert settings.RAG_QUALITY_GUARD_ENABLED is True
        assert settings.RAG_RETRIEVAL_SCORE_THRESHOLD == 0.3
        assert settings.RAG_RETRIEVAL_EXPAND_TOP_K == 10
        assert settings.RAG_RETRIEVAL_MAX_RETRIES == 1

    def test_faithfulness_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert settings.RAG_FAITHFULNESS_THRESHOLD == 3.0


# ======================================================================
# engine 集成测试
# ======================================================================


class TestEngineRetrievalGuardIntegration:
    """engine _retrieve 质量守卫集成测试。"""

    @pytest.mark.asyncio
    async def test_retrieve_triggers_quality_retry(self) -> None:
        """低重排分数时触发扩展重排。"""
        from app.rag.engine import AgenticRAGEngine

        # mock reranker — 第一次返回低分，第二次返回高分
        mock_reranker = MagicMock()
        mock_reranker.rerank = AsyncMock(
            side_effect=[
                # 第一次：低分
                [{"index": 0, "score": 0.1, "content": "doc1"}],
                # 第二次（扩展重排）：高分
                [{"index": 0, "score": 0.8, "content": "doc1"}],
            ]
        )

        mock_retriever = MagicMock()
        mock_retriever.search = AsyncMock(
            return_value=[{"doc_id": "1", "chunk_id": "c1", "content": "doc1", "score": 0.5, "source": "vector"}]
        )

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=MagicMock(),
        )

        state = {"query": "test", "iteration": 1, "retrieved_docs": []}
        await engine._retrieve(state, kb_ids=None)

        # 应该调用了 2 次 rerank（原始 + 扩展重排）
        assert mock_reranker.rerank.call_count == 2
        # 第二次调用的 top_k 应该更大
        second_call_kwargs = mock_reranker.rerank.call_args_list[1].kwargs
        assert second_call_kwargs["top_k"] > 5

    @pytest.mark.asyncio
    async def test_retrieve_no_retry_high_score(self) -> None:
        """高重排分数时不触发扩展重排。"""
        from app.rag.engine import AgenticRAGEngine

        mock_reranker = MagicMock()
        mock_reranker.rerank = AsyncMock(
            return_value=[{"index": 0, "score": 0.9, "content": "doc1"}]
        )

        mock_retriever = MagicMock()
        mock_retriever.search = AsyncMock(
            return_value=[{"doc_id": "1", "chunk_id": "c1", "content": "doc1", "score": 0.5, "source": "vector"}]
        )

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=MagicMock(),
        )

        state = {"query": "test", "iteration": 1, "retrieved_docs": []}
        await engine._retrieve(state, kb_ids=None)

        # 只调用 1 次 rerank
        assert mock_reranker.rerank.call_count == 1

    @pytest.mark.asyncio
    async def test_retrieve_no_retry_when_guard_disabled(self) -> None:
        """守卫关闭时不触发扩展重排。"""
        from app.rag.engine import AgenticRAGEngine

        mock_reranker = MagicMock()
        mock_reranker.rerank = AsyncMock(
            return_value=[{"index": 0, "score": 0.1, "content": "doc1"}]
        )

        mock_retriever = MagicMock()
        mock_retriever.search = AsyncMock(
            return_value=[{"doc_id": "1", "chunk_id": "c1", "content": "doc1", "score": 0.5, "source": "vector"}]
        )

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=MagicMock(),
        )
        # 禁用守卫
        engine._quality_guard = None

        state = {"query": "test", "iteration": 1, "retrieved_docs": []}
        await engine._retrieve(state, kb_ids=None)

        # 只调用 1 次 rerank
        assert mock_reranker.rerank.call_count == 1


# ======================================================================
# 忠实度拦截测试 — check_and_regenerate
# ======================================================================


class TestCheckAndRegenerate:
    """check_and_regenerate 方法测试。"""

    @pytest.mark.asyncio
    async def test_high_confidence_no_regen(self) -> None:
        """高置信度时不重生成。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()

        mock_eval = MagicMock()
        mock_eval.hallucination_inverse = 4  # >= 3.0 threshold
        mock_eval.error = None

        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(return_value=mock_eval)
        guard._judge_service = mock_judge

        original_answer = "原始答案"
        final_answer, eval_result = await guard.check_and_regenerate(
            query="test", answer=original_answer, contexts=["ctx"],
            generator=MagicMock(),
        )

        assert final_answer == original_answer
        assert eval_result is mock_eval

    @pytest.mark.asyncio
    async def test_low_confidence_regenerates(self) -> None:
        """低置信度时重生成答案。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()

        # 第一次评估：低分
        mock_low_eval = MagicMock()
        mock_low_eval.hallucination_inverse = 2  # < 3.0 threshold
        mock_low_eval.error = None

        # 第二次评估（重生成后）：高分
        mock_high_eval = MagicMock()
        mock_high_eval.hallucination_inverse = 4
        mock_high_eval.error = None

        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(
            side_effect=[mock_low_eval, mock_high_eval]
        )
        guard._judge_service = mock_judge

        # Mock generator
        mock_generator = MagicMock()

        async def mock_generate(*args, **kwargs):
            yield "重新生成的答案"

        mock_generator.generate = mock_generate

        original_answer = "原始低质量答案"
        final_answer, eval_result = await guard.check_and_regenerate(
            query="test", answer=original_answer, contexts=["ctx"],
            generator=mock_generator,
        )

        assert final_answer == "重新生成的答案"
        assert eval_result is mock_high_eval
        assert mock_judge.evaluate_single.call_count == 2

    @pytest.mark.asyncio
    async def test_low_confidence_no_generator(self) -> None:
        """低置信度但无 generator 时返回原答案。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()

        mock_eval = MagicMock()
        mock_eval.hallucination_inverse = 2  # low
        mock_eval.error = None

        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(return_value=mock_eval)
        guard._judge_service = mock_judge

        original_answer = "原始答案"
        final_answer, eval_result = await guard.check_and_regenerate(
            query="test", answer=original_answer, contexts=["ctx"],
            generator=None,  # no generator
        )

        assert final_answer == original_answer
        assert eval_result is mock_eval

    @pytest.mark.asyncio
    async def test_regen_empty_fallback(self) -> None:
        """重生成返回空字符串时回退到原答案。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()

        mock_eval = MagicMock()
        mock_eval.hallucination_inverse = 2  # low
        mock_eval.error = None

        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(return_value=mock_eval)
        guard._judge_service = mock_judge

        mock_generator = MagicMock()

        async def mock_generate_empty(*args, **kwargs):
            yield ""  # empty

        mock_generator.generate = mock_generate_empty

        original_answer = "原始答案"
        final_answer, eval_result = await guard.check_and_regenerate(
            query="test", answer=original_answer, contexts=["ctx"],
            generator=mock_generator,
        )

        assert final_answer == original_answer

    @pytest.mark.asyncio
    async def test_regen_exception_fallback(self) -> None:
        """重生成抛异常时回退到原答案。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()

        mock_eval = MagicMock()
        mock_eval.hallucination_inverse = 2
        mock_eval.error = None

        mock_judge = MagicMock()
        mock_judge.evaluate_single = AsyncMock(return_value=mock_eval)
        guard._judge_service = mock_judge

        mock_generator = MagicMock()
        mock_generator.generate = MagicMock(side_effect=Exception("gen error"))

        original_answer = "原始答案"
        final_answer, eval_result = await guard.check_and_regenerate(
            query="test", answer=original_answer, contexts=["ctx"],
            generator=mock_generator,
        )

        assert final_answer == original_answer

    @pytest.mark.asyncio
    async def test_guard_disabled_no_regen(self) -> None:
        """守卫关闭时不评估不重生成。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        with patch("app.rag.quality_guard.get_settings") as mock_settings:
            mock_settings.return_value.RAG_QUALITY_GUARD_ENABLED = False
            mock_settings.return_value.RAG_FAITHFULNESS_THRESHOLD = 3.0
            final_answer, eval_result = await guard.check_and_regenerate(
                query="test", answer="answer", contexts=["ctx"],
                generator=MagicMock(),
            )
            assert final_answer == "answer"
            assert eval_result is None


# ======================================================================
# engine 集成测试
# ======================================================================


class TestEngineReflectUpgradeIntegration:
    """engine _reflect 升级集成测试（含幻觉防护流水线）。"""

    @pytest.mark.asyncio
    async def test_reflect_uses_judge_service(self) -> None:
        """_reflect 调用 check_and_regenerate 进行忠实度评估。"""
        from app.rag.engine import AgenticRAGEngine

        mock_eval_result = MagicMock()
        mock_eval_result.citation_accuracy = 4
        mock_eval_result.completeness = 5
        mock_eval_result.hallucination_inverse = 4
        mock_eval_result.total_score = 4.33
        mock_eval_result.passed = True
        mock_eval_result.error = None

        mock_guard = MagicMock()
        mock_guard.check_and_regenerate = AsyncMock(
            return_value=("测试答案", mock_eval_result)
        )
        mock_guard.is_low_confidence = MagicMock(return_value=False)

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=MagicMock(),
            reranker=MagicMock(),
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {
            "query": "测试问题",
            "answer": "测试答案",
            "retrieved_docs": [{"content": "文档1"}],
            "iteration": 1,
            "session_id": "s1",
        }

        result = await engine._reflect(state)

        assert result is not None
        mock_guard.check_and_regenerate.assert_called_once()
        assert state["eval_result"] is mock_eval_result
        assert state["low_confidence"] is False

    @pytest.mark.asyncio
    async def test_reflect_marks_low_confidence(self) -> None:
        """faithfulness 低于阈值时标记 low_confidence。"""
        from app.rag.engine import AgenticRAGEngine

        mock_eval_result = MagicMock()
        mock_eval_result.hallucination_inverse = 2  # 低分
        mock_eval_result.error = None

        mock_guard = MagicMock()
        mock_guard.check_and_regenerate = AsyncMock(
            return_value=("answer", mock_eval_result)
        )
        mock_guard.is_low_confidence = MagicMock(return_value=True)

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=MagicMock(),
            reranker=MagicMock(),
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {
            "query": "test",
            "answer": "answer",
            "retrieved_docs": [],
            "iteration": 1,
            "session_id": "s1",
        }

        await engine._reflect(state)

        assert state["low_confidence"] is True

    @pytest.mark.asyncio
    async def test_reflect_fallback_to_inline(self) -> None:
        """quality_guard 为 None 时降级到 inline reflect。"""
        from app.rag.engine import AgenticRAGEngine

        mock_llm = MagicMock()
        async def mock_chat(*args, **kwargs):
            for chunk in ["satisfied"]:
                yield chunk

        mock_llm.chat = mock_chat

        engine = AgenticRAGEngine(
            llm=mock_llm,
            mcp_client=MagicMock(),
            retriever=MagicMock(),
            reranker=MagicMock(),
            generator=MagicMock(),
        )
        engine._quality_guard = None

        state = {
            "query": "test",
            "answer": "test answer",
            "iteration": 1,
            "session_id": "s1",
        }

        result = await engine._reflect(state)
        assert result is None  # 降级路径返回 None

    @pytest.mark.asyncio
    async def test_reflect_empty_answer(self) -> None:
        """空答案时返回 None。"""
        from app.rag.engine import AgenticRAGEngine

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=MagicMock(),
            reranker=MagicMock(),
            generator=MagicMock(),
            quality_guard=MagicMock(),
        )

        state = {"query": "test", "answer": "", "iteration": 1, "session_id": "s1"}
        result = await engine._reflect(state)
        assert result is None

    @pytest.mark.asyncio
    async def test_reflect_runs_hallucination_checks(self) -> None:
        """_reflect 执行引用校验、矛盾检测和高风险核验。"""
        from app.rag.engine import AgenticRAGEngine

        mock_eval_result = MagicMock()
        mock_eval_result.hallucination_inverse = 4
        mock_eval_result.error = None

        mock_guard = MagicMock()
        mock_guard.check_and_regenerate = AsyncMock(
            return_value=("答案[1]", mock_eval_result)
        )
        mock_guard.is_low_confidence = MagicMock(return_value=False)

        mock_llm = MagicMock()

        engine = AgenticRAGEngine(
            llm=mock_llm,
            mcp_client=MagicMock(),
            retriever=MagicMock(),
            reranker=MagicMock(),
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {
            "query": "合同金额是多少？",
            "answer": "根据文档[1]，合同金额为 50000元",
            "retrieved_docs": [{"content": "合同金额为 50000元", "doc_id": "1"}],
            "iteration": 1,
            "session_id": "s1",
        }

        await engine._reflect(state)

        # 引用校验结果应存入 state
        assert "citation_validation" in state
        assert state["citation_validation"]["valid"] is True

        # 高风险核验结果应存入 state
        assert "high_risk_result" in state

    @pytest.mark.asyncio
    async def test_reflect_citation_invalid_marked(self) -> None:
        """答案无引用标注时标记 citation_invalid。"""
        from app.rag.engine import AgenticRAGEngine

        mock_eval_result = MagicMock()
        mock_eval_result.hallucination_inverse = 4
        mock_eval_result.error = None

        mock_guard = MagicMock()
        mock_guard.check_and_regenerate = AsyncMock(
            return_value=("无引用答案", mock_eval_result)
        )
        mock_guard.is_low_confidence = MagicMock(return_value=False)

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=MagicMock(),
            reranker=MagicMock(),
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {
            "query": "产品功能是什么？",
            "answer": "该产品支持批量导入功能。",  # 无 [n] 引用
            "retrieved_docs": [{"content": "产品支持批量导入", "doc_id": "1"}],
            "iteration": 1,
            "session_id": "s1",
        }

        await engine._reflect(state)

        assert state.get("citation_invalid") is True
        assert state["citation_validation"]["valid"] is False
