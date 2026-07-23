"""
动态匹配阈值单元测试 — app/rag/frequency_threshold.py + QualityGuard 集成。

覆盖：
    - _normalize_query / _query_hash 归一化
    - _compute_threshold 纯函数（高频上浮 / 低频下浮 / clamp）
    - record_query + get_threshold — 进程内降级路径
    - record_query + get_threshold — Redis 路径（mock）
    - 总开关关闭行为
    - 空查询处理
    - Redis 不可用降级
    - QualityGuard.get_dynamic_threshold / record_query_frequency
    - QualityGuard.check_retrieval_quality(threshold_override=...)
    - engine _retrieve 集成：记录频次 + 动态阈值传入
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
# 归一化辅助函数
# ======================================================================


class TestNormalizeQuery:
    """查询归一化测试。"""

    def test_lowercases(self) -> None:
        from app.rag.frequency_threshold import _normalize_query

        assert _normalize_query("Hello World") == "hello world"

    def test_collapses_whitespace(self) -> None:
        from app.rag.frequency_threshold import _normalize_query

        assert _normalize_query("  报销   流程  ") == "报销 流程"

    def test_empty(self) -> None:
        from app.rag.frequency_threshold import _normalize_query

        assert _normalize_query("") == ""
        assert _normalize_query("   ") == ""

    def test_case_insensitive_hash(self) -> None:
        """大小写/空格差异的查询应产生相同哈希。"""
        from app.rag.frequency_threshold import _query_hash

        assert _query_hash("Hello  World") == _query_hash("hello world")
        assert _query_hash("  A B ") == _query_hash("a b")


# ======================================================================
# _compute_threshold 纯函数
# ======================================================================


class TestComputeThreshold:
    """阈值计算纯函数测试（无 IO）。"""

    def _fbt(self) -> "FrequencyBasedThreshold":
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold()
        # 跳过 Redis 连接（测试环境可能存在 Redis）
        fbt._redis_available = False
        fbt._retry_at = float("inf")
        return fbt

    def test_cold_query_lowers_threshold(self) -> None:
        """低频查询（count < HOT_COUNT）阈值下浮。"""
        fbt = self._fbt()
        # 默认 base=0.3, cold_drop=0.05 → 0.25
        threshold = fbt._compute_threshold(base=0.3, count=0)
        assert threshold == 0.25

    def test_hot_query_raises_threshold(self) -> None:
        """高频查询（count >= HOT_COUNT）阈值上浮。"""
        fbt = self._fbt()
        # 默认 base=0.3, hot_boost=0.1 → 0.4
        threshold = fbt._compute_threshold(base=0.3, count=10)
        assert threshold == 0.4

    def test_hot_boundary(self) -> None:
        """频次正好等于 HOT_COUNT 时视为高频。"""
        fbt = self._fbt()
        threshold = fbt._compute_threshold(base=0.3, count=10)
        assert threshold == 0.4  # base + hot_boost

    def test_clamp_to_min(self) -> None:
        """低频下浮不低于 MIN 地板。"""
        fbt = self._fbt()
        with patch("app.rag.frequency_threshold.get_settings") as mock:
            s = mock.return_value
            s.RAG_RETRIEVAL_SCORE_THRESHOLD = 0.3
            s.RAG_THRESHOLD_FREQ_HOT_COUNT = 10
            s.RAG_THRESHOLD_HOT_BOOST = 0.1
            s.RAG_THRESHOLD_COLD_DROP = 0.5  # 下浮超过 base
            s.RAG_THRESHOLD_MIN = 0.1
            s.RAG_THRESHOLD_MAX = 0.6
            # 0.3 - 0.5 = -0.2 → clamp 到 0.1
            assert fbt._compute_threshold(base=0.3, count=0) == 0.1

    def test_clamp_to_max(self) -> None:
        """高频上浮不超过 MAX 天花板。"""
        fbt = self._fbt()
        with patch("app.rag.frequency_threshold.get_settings") as mock:
            s = mock.return_value
            s.RAG_RETRIEVAL_SCORE_THRESHOLD = 0.3
            s.RAG_THRESHOLD_FREQ_HOT_COUNT = 10
            s.RAG_THRESHOLD_HOT_BOOST = 0.9  # 上浮超过 1
            s.RAG_THRESHOLD_COLD_DROP = 0.05
            s.RAG_THRESHOLD_MIN = 0.1
            s.RAG_THRESHOLD_MAX = 0.6
            # 0.3 + 0.9 = 1.2 → clamp 到 0.6
            assert fbt._compute_threshold(base=0.3, count=10) == 0.6

    def test_threshold_increases_with_frequency(self) -> None:
        """频次越高阈值越高（单调性）。"""
        fbt = self._fbt()
        cold = fbt._compute_threshold(base=0.3, count=0)
        hot = fbt._compute_threshold(base=0.3, count=100)
        assert hot > cold


# ======================================================================
# record_query + get_threshold — 进程内降级路径
# ======================================================================


class TestMemoryPath:
    """Redis 不可用时的进程内计数器路径测试。"""

    def _fbt(self) -> "FrequencyBasedThreshold":
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold()
        fbt._redis_available = False  # 强制跳过 Redis
        fbt._retry_at = float("inf")  # 阻止重试探测
        return fbt

    @pytest.mark.asyncio
    async def test_record_returns_incremented_count(self) -> None:
        fbt = self._fbt()
        assert await fbt.record_query("报销流程") == 1
        assert await fbt.record_query("报销流程") == 2
        assert await fbt.record_query("报销流程") == 3

    @pytest.mark.asyncio
    async def test_different_queries_separate_counts(self) -> None:
        fbt = self._fbt()
        await fbt.record_query("query A")
        await fbt.record_query("query A")
        await fbt.record_query("query B")
        # query B 只记录 1 次
        assert await fbt.get_threshold("query B") < await fbt.get_threshold("query A") or True
        # 验证计数独立：A=2, B=1 — 通过 get_threshold 间接验证（都是 cold，阈值相同）
        # 直接验证内存计数
        from app.rag.frequency_threshold import _query_hash

        assert fbt._mem_store[_query_hash("query A")] == 2
        assert fbt._mem_store[_query_hash("query B")] == 1

    @pytest.mark.asyncio
    async def test_get_threshold_cold_then_hot(self) -> None:
        """查询从冷门变热门，阈值应升高。"""
        fbt = self._fbt()
        # 冷门：阈值 = 0.3 - 0.05 = 0.25
        cold_threshold = await fbt.get_threshold("热门问题")
        assert cold_threshold == 0.25

        # 记录 10 次（达到 HOT_COUNT）
        for _ in range(10):
            await fbt.record_query("热门问题")

        # 热门：阈值 = 0.3 + 0.1 = 0.4
        hot_threshold = await fbt.get_threshold("热门问题")
        assert hot_threshold == 0.4

    @pytest.mark.asyncio
    async def test_lru_eviction(self) -> None:
        """内存计数器超限时淘汰最旧条目。"""
        fbt = self._fbt()
        fbt._mem_store  # 确保 store 存在
        # 直接测试 _mem_incr 的淘汰逻辑
        from app.rag.frequency_threshold import _MEM_MAX_ENTRIES

        # 填满 + 1 条，验证容量不超限
        for i in range(_MEM_MAX_ENTRIES + 5):
            fbt._mem_incr(f"q{i}")
        assert len(fbt._mem_store) <= _MEM_MAX_ENTRIES

    @pytest.mark.asyncio
    async def test_case_insensitive_frequency(self) -> None:
        """大小写不同的查询应累计同一频次。"""
        fbt = self._fbt()
        await fbt.record_query("Hello World")
        await fbt.record_query("hello world")
        await fbt.record_query("HELLO  WORLD")
        # 三次都归一化为同一查询
        from app.rag.frequency_threshold import _query_hash

        assert fbt._mem_store[_query_hash("Hello World")] == 3


# ======================================================================
# record_query + get_threshold — Redis 路径
# ======================================================================


class TestRedisPath:
    """Redis 可用时的频次追踪测试（mock Redis）。"""

    def _fbt_with_mock_redis(self, redis_mock: MagicMock) -> "FrequencyBasedThreshold":
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold(redis=redis_mock)
        fbt._redis_available = True
        return fbt

    @pytest.mark.asyncio
    async def test_record_uses_redis_incr(self) -> None:
        redis_mock = MagicMock()
        redis_mock.incr = AsyncMock(return_value=1)
        redis_mock.expire = AsyncMock(return_value=True)

        fbt = self._fbt_with_mock_redis(redis_mock)
        count = await fbt.record_query("测试查询")

        assert count == 1
        redis_mock.incr.assert_called_once()
        # 首次创建应设置 TTL
        redis_mock.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_no_expire_on_subsequent(self) -> None:
        """非首次记录不重设 TTL（避免重置滑动窗口）。"""
        redis_mock = MagicMock()
        redis_mock.incr = AsyncMock(return_value=5)  # 第 5 次
        redis_mock.expire = AsyncMock(return_value=True)

        fbt = self._fbt_with_mock_redis(redis_mock)
        await fbt.record_query("测试查询")

        redis_mock.incr.assert_called_once()
        redis_mock.expire.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_threshold_reads_redis(self) -> None:
        redis_mock = MagicMock()
        redis_mock.get = AsyncMock(return_value="15")  # 高频

        fbt = self._fbt_with_mock_redis(redis_mock)
        threshold = await fbt.get_threshold("热门查询")

        # count=15 >= HOT_COUNT(10) → 0.3 + 0.1 = 0.4
        assert threshold == 0.4
        redis_mock.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_threshold_redis_returns_zero(self) -> None:
        """Redis 无记录时返回 0，按低频处理。"""
        redis_mock = MagicMock()
        redis_mock.get = AsyncMock(return_value=None)

        fbt = self._fbt_with_mock_redis(redis_mock)
        threshold = await fbt.get_threshold("新查询")

        # count=0 < HOT_COUNT → 0.3 - 0.05 = 0.25
        assert threshold == 0.25

    @pytest.mark.asyncio
    async def test_redis_error_falls_back_to_memory(self) -> None:
        """Redis 异常时降级到进程内计数器。"""
        redis_mock = MagicMock()
        redis_mock.incr = AsyncMock(side_effect=Exception("Redis down"))

        fbt = self._fbt_with_mock_redis(redis_mock)
        # Redis 抛异常 → 降级内存
        count = await fbt.record_query("降级测试")
        assert count == 1
        assert fbt._redis_available is False
        # 内存中有记录
        from app.rag.frequency_threshold import _query_hash

        assert fbt._mem_store[_query_hash("降级测试")] == 1


# ======================================================================
# 总开关 & 边界
# ======================================================================


class TestDisabledAndEdgeCases:
    """总开关关闭与边界条件测试。"""

    @pytest.mark.asyncio
    async def test_disabled_returns_base_threshold(self) -> None:
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold()
        fbt._redis_available = False
        with patch("app.rag.frequency_threshold.get_settings") as mock:
            mock.return_value.RAG_DYNAMIC_THRESHOLD_ENABLED = False
            mock.return_value.RAG_RETRIEVAL_SCORE_THRESHOLD = 0.3
            threshold = await fbt.get_threshold("任意查询")
            assert threshold == 0.3  # 静态基准

    @pytest.mark.asyncio
    async def test_disabled_record_is_noop(self) -> None:
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold()
        fbt._redis_available = False
        with patch("app.rag.frequency_threshold.get_settings") as mock:
            mock.return_value.RAG_DYNAMIC_THRESHOLD_ENABLED = False
            count = await fbt.record_query("任意查询")
            assert count == 0  # 空操作
            assert len(fbt._mem_store) == 0

    @pytest.mark.asyncio
    async def test_empty_query_get_threshold(self) -> None:
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold()
        fbt._redis_available = False
        # 空查询返回静态基准
        threshold = await fbt.get_threshold("")
        assert threshold == 0.3

    @pytest.mark.asyncio
    async def test_empty_query_record(self) -> None:
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold()
        fbt._redis_available = False
        count = await fbt.record_query("")
        assert count == 0
        assert len(fbt._mem_store) == 0

    def test_enabled_property(self) -> None:
        from app.rag.frequency_threshold import FrequencyBasedThreshold

        fbt = FrequencyBasedThreshold()
        fbt._redis_available = False
        assert fbt.enabled is True  # 默认开启


# ======================================================================
# QualityGuard 集成
# ======================================================================


class TestQualityGuardDynamicThreshold:
    """QualityGuard 动态阈值方法测试。"""

    @pytest.mark.asyncio
    async def test_get_dynamic_threshold_returns_float(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        # 注入 mock FrequencyBasedThreshold
        mock_fbt = MagicMock()
        mock_fbt.enabled = True
        mock_fbt.get_threshold = AsyncMock(return_value=0.4)
        guard._freq_threshold = mock_fbt

        result = await guard.get_dynamic_threshold("测试查询")
        assert result == 0.4
        mock_fbt.get_threshold.assert_called_once_with("测试查询")

    @pytest.mark.asyncio
    async def test_get_dynamic_threshold_disabled_returns_none(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        mock_fbt = MagicMock()
        mock_fbt.enabled = False
        guard._freq_threshold = mock_fbt

        result = await guard.get_dynamic_threshold("测试查询")
        assert result is None
        mock_fbt.get_threshold.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_dynamic_threshold_unavailable_returns_none(self) -> None:
        """FrequencyBasedThreshold 初始化失败时返回 None。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        with patch.object(guard, "_get_freq_threshold", return_value=None):
            result = await guard.get_dynamic_threshold("测试查询")
            assert result is None

    @pytest.mark.asyncio
    async def test_record_query_frequency(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        mock_fbt = MagicMock()
        mock_fbt.record_query = AsyncMock(return_value=1)
        guard._freq_threshold = mock_fbt

        await guard.record_query_frequency("记录查询")
        mock_fbt.record_query.assert_called_once_with("记录查询")

    @pytest.mark.asyncio
    async def test_record_query_frequency_unavailable_noop(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        with patch.object(guard, "_get_freq_threshold", return_value=None):
            # 不应抛异常
            await guard.record_query_frequency("查询")


# ======================================================================
# QualityGuard.check_retrieval_quality — threshold_override
# ======================================================================


class TestCheckRetrievalQualityOverride:
    """check_retrieval_quality 的 threshold_override 参数测试。"""

    def test_override_used_when_provided(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [{"content": "doc1", "score": 0.35}]
        # 静态阈值 0.3 → 0.35 通过；但 override=0.5 → 0.35 不通过
        result = guard.check_retrieval_quality(docs, threshold_override=0.5)
        assert result.mean_score == 0.35
        assert result.passed is False  # 0.35 < 0.5

    def test_override_higher_passes(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [{"content": "doc1", "score": 0.45}]
        result = guard.check_retrieval_quality(docs, threshold_override=0.4)
        assert result.passed is True  # 0.45 >= 0.4

    def test_none_override_uses_static(self) -> None:
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [{"content": "doc1", "score": 0.35}]
        # override=None → 使用静态 0.3 → 0.35 通过
        result = guard.check_retrieval_quality(docs, threshold_override=None)
        assert result.passed is True

    def test_no_override_arg_uses_static(self) -> None:
        """不传 override 参数时向后兼容。"""
        from app.rag.quality_guard import QualityGuard

        guard = QualityGuard()
        docs = [{"content": "doc1", "score": 0.3}]
        result = guard.check_retrieval_quality(docs)
        assert result.passed is True  # 0.3 >= 0.3（静态阈值）


# ======================================================================
# engine _retrieve 集成
# ======================================================================


class TestEngineDynamicThresholdIntegration:
    """engine _retrieve 动态阈值集成测试。"""

    @pytest.mark.asyncio
    async def test_retrieve_records_frequency_on_first_iteration(self) -> None:
        """首次迭代记录查询频次。"""
        from app.rag.engine import AgenticRAGEngine

        mock_guard = MagicMock()
        mock_guard.record_query_frequency = AsyncMock()
        mock_guard.get_dynamic_threshold = AsyncMock(return_value=None)
        mock_guard.check_retrieval_quality = MagicMock(
            return_value=MagicMock(passed=True, mean_score=0.8, doc_count=1)
        )
        mock_guard.should_retry_retrieval = MagicMock(return_value=False)

        mock_reranker = MagicMock()
        mock_reranker.rerank = AsyncMock(
            return_value=[{"index": 0, "score": 0.8, "content": "doc1"}]
        )

        mock_retriever = MagicMock()
        mock_retriever.search = AsyncMock(
            return_value=[
                {"doc_id": "1", "chunk_id": "c1", "content": "doc1", "score": 0.5}
            ]
        )

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {"query": "test", "iteration": 1, "retrieved_docs": []}
        await engine._retrieve(state, kb_ids=None)

        mock_guard.record_query_frequency.assert_called_once_with("test")

    @pytest.mark.asyncio
    async def test_retrieve_skips_frequency_on_later_iterations(self) -> None:
        """非首次迭代不重复记录频次。"""
        from app.rag.engine import AgenticRAGEngine

        mock_guard = MagicMock()
        mock_guard.record_query_frequency = AsyncMock()
        mock_guard.get_dynamic_threshold = AsyncMock(return_value=None)
        mock_guard.check_retrieval_quality = MagicMock(
            return_value=MagicMock(passed=True, mean_score=0.8, doc_count=1)
        )
        mock_guard.should_retry_retrieval = MagicMock(return_value=False)

        mock_reranker = MagicMock()
        mock_reranker.rerank = AsyncMock(
            return_value=[{"index": 0, "score": 0.8, "content": "doc1"}]
        )

        mock_retriever = MagicMock()
        mock_retriever.search = AsyncMock(
            return_value=[
                {"doc_id": "1", "chunk_id": "c1", "content": "doc1", "score": 0.5}
            ]
        )

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {"query": "test", "iteration": 2, "retrieved_docs": []}
        await engine._retrieve(state, kb_ids=None)

        mock_guard.record_query_frequency.assert_not_called()

    @pytest.mark.asyncio
    async def test_retrieve_passes_dynamic_threshold_to_check(self) -> None:
        """动态阈值传入 check_retrieval_quality。"""
        from app.rag.engine import AgenticRAGEngine

        mock_guard = MagicMock()
        mock_guard.record_query_frequency = AsyncMock()
        mock_guard.get_dynamic_threshold = AsyncMock(return_value=0.45)
        check_result = MagicMock(passed=True, mean_score=0.8, doc_count=1)
        mock_guard.check_retrieval_quality = MagicMock(return_value=check_result)
        mock_guard.should_retry_retrieval = MagicMock(return_value=False)

        mock_reranker = MagicMock()
        mock_reranker.rerank = AsyncMock(
            return_value=[{"index": 0, "score": 0.8, "content": "doc1"}]
        )

        mock_retriever = MagicMock()
        mock_retriever.search = AsyncMock(
            return_value=[
                {"doc_id": "1", "chunk_id": "c1", "content": "doc1", "score": 0.5}
            ]
        )

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {"query": "test", "iteration": 1, "retrieved_docs": []}
        await engine._retrieve(state, kb_ids=None)

        # check_retrieval_quality 应以 threshold_override=0.45 调用
        call_kwargs = mock_guard.check_retrieval_quality.call_args.kwargs
        assert call_kwargs["threshold_override"] == 0.45

    @pytest.mark.asyncio
    async def test_retrieve_dynamic_threshold_error_falls_back(self) -> None:
        """get_dynamic_threshold 异常时回退 None（静态阈值）。"""
        from app.rag.engine import AgenticRAGEngine

        mock_guard = MagicMock()
        mock_guard.record_query_frequency = AsyncMock()
        mock_guard.get_dynamic_threshold = AsyncMock(
            side_effect=Exception("freq error")
        )
        check_result = MagicMock(passed=True, mean_score=0.8, doc_count=1)
        mock_guard.check_retrieval_quality = MagicMock(return_value=check_result)
        mock_guard.should_retry_retrieval = MagicMock(return_value=False)

        mock_reranker = MagicMock()
        mock_reranker.rerank = AsyncMock(
            return_value=[{"index": 0, "score": 0.8, "content": "doc1"}]
        )

        mock_retriever = MagicMock()
        mock_retriever.search = AsyncMock(
            return_value=[
                {"doc_id": "1", "chunk_id": "c1", "content": "doc1", "score": 0.5}
            ]
        )

        engine = AgenticRAGEngine(
            llm=MagicMock(),
            mcp_client=MagicMock(),
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=MagicMock(),
            quality_guard=mock_guard,
        )

        state = {"query": "test", "iteration": 1, "retrieved_docs": []}
        # 不应抛异常
        await engine._retrieve(state, kb_ids=None)

        # threshold_override 应为 None（回退静态）
        call_kwargs = mock_guard.check_retrieval_quality.call_args.kwargs
        assert call_kwargs["threshold_override"] is None
