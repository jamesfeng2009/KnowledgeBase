"""
Mem0 语义检索测试 — app/memory/mem0_manager.py。

覆盖范围：
    - _cosine_similarity 辅助函数
    - search_facts 语义检索（向量 embedding + 余弦相似度排序）
    - search_facts 关键词降级（Embedder 不可用时）
    - search_facts 无 query 时按时间排序
    - add_fact 向量嵌入生成
"""

from __future__ import annotations

import math
import sys
import uuid
from datetime import datetime, timedelta
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
# _cosine_similarity 辅助函数测试
# ======================================================================


class TestCosineSimilarity:
    """_cosine_similarity 函数测试。"""

    def test_identical_vectors(self) -> None:
        from app.memory.mem0_manager import _cosine_similarity

        vec = [1.0, 2.0, 3.0]
        assert _cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        from app.memory.mem0_manager import _cosine_similarity

        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        from app.memory.mem0_manager import _cosine_similarity

        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_vectors(self) -> None:
        from app.memory.mem0_manager import _cosine_similarity

        assert _cosine_similarity([], [1.0, 2.0]) == 0.0
        assert _cosine_similarity([1.0], []) == 0.0

    def test_zero_vector(self) -> None:
        from app.memory.mem0_manager import _cosine_similarity

        assert _cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_partial_similarity(self) -> None:
        from app.memory.mem0_manager import _cosine_similarity

        # [1,0] vs [1,1] → cos = 1/sqrt(2) ≈ 0.707
        sim = _cosine_similarity([1.0, 0.0], [1.0, 1.0])
        assert 0.6 < sim < 0.8


# ======================================================================
# search_facts 语义检索测试
# ======================================================================


class TestSearchFactsSemantic:
    """search_facts 语义检索测试。"""

    def _make_fact(self, text: str, embedding: list[float] | None = None) -> MagicMock:
        """创建模拟 MemoryFact 对象。"""
        fact = MagicMock()
        fact.fact_text = text
        fact.embedding = embedding
        fact.is_active = True
        fact.category = "working"
        fact.fact_key = None
        fact.fact_value = None
        fact.created_at = datetime.utcnow()
        fact.expires_at = None
        fact.access_count = 0
        fact.last_accessed_at = None
        fact.superseded_by = None
        fact.superseded_at = None
        return fact

    @pytest.mark.asyncio
    async def test_semantic_search_returns_relevant(self) -> None:
        """语义检索返回相似度高于阈值的事实。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        fact1 = self._make_fact("用户偏好简洁回答", embedding=[1.0, 0.0, 0.0])
        fact2 = self._make_fact("报销单 BG001 已提交", embedding=[0.0, 1.0, 0.0])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact1, fact2]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        # Mock embedder 返回与 fact1 相似的向量
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="用户偏好是什么",
            limit=10,
        )

        # fact1 相似度=1.0 > 0.3，fact2 相似度=0.0 < 0.3
        assert fact1 in results
        assert fact2 not in results

    @pytest.mark.asyncio
    async def test_semantic_search_sorted_by_similarity(self) -> None:
        """语义检索结果按相似度降序排序。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        # fact_low 相似度较低，fact_high 相似度较高
        fact_low = self._make_fact("天气晴朗", embedding=[1.0, 0.1, 0.0])
        fact_high = self._make_fact("用户喜欢简洁回答", embedding=[0.99, 0.01, 0.0])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact_low, fact_high]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="用户偏好",
            limit=10,
        )

        # fact_high 相似度更高应排在前面
        assert results[0] == fact_high
        assert results[1] == fact_low

    @pytest.mark.asyncio
    async def test_semantic_search_no_match_fallback_keyword(self) -> None:
        """语义检索无匹配时降级到关键词匹配。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        fact = self._make_fact("报销单已审批", embedding=[0.0, 1.0, 0.0])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        # query 向量与 fact 正交（相似度=0），无语义匹配
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="报销单",
            limit=10,
        )

        # 语义无匹配 → 关键词 "报销单" 在 fact_text 中 → 关键词匹配返回
        assert len(results) == 1
        assert results[0] == fact

    @pytest.mark.asyncio
    async def test_embedder_unavailable_fallback_keyword(self) -> None:
        """Embedder 不可用时降级到关键词匹配。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        fact1 = self._make_fact("用户偏好中文", embedding=None)
        fact2 = self._make_fact("报销单已提交", embedding=None)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact1, fact2]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(side_effect=Exception("embedder unavailable"))

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="偏好",
            limit=10,
        )

        # Embedder 异常 → 关键词匹配 "偏好" 在 fact1 中
        assert fact1 in results
        assert fact2 not in results

    @pytest.mark.asyncio
    async def test_no_query_returns_recent(self) -> None:
        """无 query 时按时间排序返回最近事实。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        fact1 = self._make_fact("事实1", embedding=[1.0])
        fact2 = self._make_fact("事实2", embedding=[2.0])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact1, fact2]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query=None,
            limit=10,
        )

        # 无 query → 直接返回，不做语义/关键词过滤
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_empty_facts_returns_empty(self) -> None:
        """无事实时返回空列表。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="test",
            limit=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_partial_embedding_fallback(self) -> None:
        """部分事实有 embedding，部分没有时混合处理。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        fact_with_emb = self._make_fact("偏好简洁", embedding=[1.0, 0.0])
        fact_no_emb = self._make_fact("偏好中文回复", embedding=None)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact_with_emb, fact_no_emb]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="偏好",
            limit=10,
        )

        # fact_with_emb 语义匹配（相似度=1.0），fact_no_emb 关键词匹配
        assert fact_with_emb in results
        assert fact_no_emb in results

    @pytest.mark.asyncio
    async def test_similarity_threshold_filters(self) -> None:
        """相似度阈值过滤低相似度事实。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        # 相似度 ≈ 0.1（低于默认阈值 0.3）
        fact_low = self._make_fact("不相关内容", embedding=[0.1, 0.99, 0.0])
        # 相似度 = 1.0
        fact_high = self._make_fact("完全匹配", embedding=[1.0, 0.0, 0.0])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact_low, fact_high]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="测试",
            limit=10,
            similarity_threshold=0.3,
        )

        # fact_low 相似度 ≈ 0.1 < 0.3 被过滤
        assert fact_high in results
        assert fact_low not in results


# ======================================================================
# 时间衰减因子测试
# ======================================================================


class TestTimeDecay:
    """_time_decay 函数测试。"""

    def test_no_decay_when_disabled(self) -> None:
        """half_life=0 时禁用衰减。"""
        from app.memory.mem0_manager import _time_decay

        old_time = datetime(2020, 1, 1)
        assert _time_decay(old_time, 1700000000.0, 0) == 1.0

    def test_no_decay_for_none_created_at(self) -> None:
        """created_at 为 None 时不衰减。"""
        from app.memory.mem0_manager import _time_decay

        assert _time_decay(None, 1700000000.0, 30.0) == 1.0

    def test_recent_fact_high_weight(self) -> None:
        """刚创建的事实衰减小（接近 1.0）。"""
        from app.memory.mem0_manager import _time_decay

        now = datetime.utcnow()
        decay = _time_decay(now, now.timestamp(), 30.0)
        assert decay == pytest.approx(1.0, abs=0.01)

    def test_half_life_decay(self) -> None:
        """半衰期时衰减约 0.5。"""
        from app.memory.mem0_manager import _time_decay

        created = datetime(2024, 1, 1)
        now_ts = created.timestamp() + 30 * 86400  # 30 天后
        decay = _time_decay(created, now_ts, half_life_days=30.0)
        assert decay == pytest.approx(0.5, abs=0.01)

    def test_double_half_life_decay(self) -> None:
        """两倍半衰期时衰减约 0.25。"""
        from app.memory.mem0_manager import _time_decay

        created = datetime(2024, 1, 1)
        now_ts = created.timestamp() + 60 * 86400  # 60 天后
        decay = _time_decay(created, now_ts, half_life_days=30.0)
        assert decay == pytest.approx(0.25, abs=0.01)

    def test_very_old_fact_low_weight(self) -> None:
        """非常旧的事实衰减接近 0。"""
        from app.memory.mem0_manager import _time_decay

        created = datetime(2020, 1, 1)
        now_ts = created.timestamp() + 365 * 86400  # 1 年后
        decay = _time_decay(created, now_ts, half_life_days=30.0)
        assert decay < 0.01

    def test_future_time_no_decay(self) -> None:
        """未来时间不衰减。"""
        from app.memory.mem0_manager import _time_decay

        created = datetime(2025, 6, 1)
        now_ts = created.timestamp() - 86400  # 创建时间比 now 晚
        assert _time_decay(created, now_ts, 30.0) == 1.0


class TestTimeDecaySearch:
    """search_facts 时间衰减集成测试。"""

    def _make_fact(
        self,
        text: str,
        embedding: list[float] | None = None,
        created_at: datetime | None = None,
        access_count: int = 0,
    ) -> MagicMock:
        fact = MagicMock()
        fact.fact_text = text
        fact.embedding = embedding
        fact.is_active = True
        fact.category = "working"
        fact.fact_key = None
        fact.fact_value = None
        fact.created_at = created_at or datetime.utcnow()
        fact.expires_at = None
        fact.access_count = access_count
        fact.last_accessed_at = None
        fact.superseded_by = None
        fact.superseded_at = None
        return fact

    @pytest.mark.asyncio
    async def test_recent_fact_ranks_higher(self) -> None:
        """相同语义相似度下，近期事实排名更高。

        老事实（90 天）靠频率增益（access_count）活过激活值地板，
        但时间衰减仍让它排在后面 — ACT-R 三因子综合排序。
        """
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        now = datetime.utcnow()
        old_date = now - timedelta(days=90)

        fact_old = self._make_fact(
            "用户偏好简洁",
            embedding=[1.0, 0.0],
            created_at=old_date,
            access_count=10,
        )
        fact_new = self._make_fact("用户偏好简洁", embedding=[1.0, 0.0], created_at=now)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact_old, fact_new]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="偏好",
            limit=10,
        )

        # 相同相似度：新事实满激活排前，老事实靠频率增益存活但排后
        assert results[0] == fact_new
        assert results[1] == fact_old

    @pytest.mark.asyncio
    async def test_old_and_cold_fact_filtered_by_floor(self) -> None:
        """老且无人问津的事实：激活值跌破地板，当场跳过（机制一闸门）。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        now = datetime.utcnow()
        old_date = now - timedelta(days=90)

        fact_old = self._make_fact(
            "用户偏好简洁", embedding=[1.0, 0.0], created_at=old_date
        )
        fact_new = self._make_fact("用户偏好简洁", embedding=[1.0, 0.0], created_at=now)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact_old, fact_new]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="偏好",
            limit=10,
        )

        assert results == [fact_new]

    @pytest.mark.asyncio
    async def test_decay_disabled_when_zero(self, monkeypatch) -> None:
        """激活值闸门关闭时纯按相似度排序（退回旧行为的逃生门）。"""
        from app.config import get_settings
        from app.memory.mem0_manager import Mem0Manager

        settings = get_settings()
        monkeypatch.setattr(
            settings, "MEMORY_ACTIVATION_ENABLED", False, raising=False
        )

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        now = datetime.utcnow()
        old_date = now - timedelta(days=90)

        fact_old = self._make_fact(
            "用户偏好简洁", embedding=[1.0, 0.0], created_at=old_date
        )
        fact_new = self._make_fact("用户偏好简洁", embedding=[1.0, 0.0], created_at=now)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [fact_old, fact_new]
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])

        results = await manager.search_facts(
            user_id=uuid.uuid4(),
            query="偏好",
            limit=10,
        )

        # 闸门关闭时，相同相似度按原始顺序（DB 返回顺序）
        assert len(results) == 2
