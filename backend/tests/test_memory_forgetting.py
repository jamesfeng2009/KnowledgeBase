"""
记忆遗忘机制测试 — 课程07《敢遗忘才是解药》全量移植。

覆盖范围：
    - forgetting.py：ACT-R 三因子激活值（时间衰减 + 频率增益 + 近期增益）
    - mem0_manager：激活值闸门、复活窗口、召回命中写回、supersede 标记
    - conflict_arbiter：Top-K 检索分层、LLM 裁决、规则短路、fail-open
    - memory_manager：_consolidated_add 写入编排与降级
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

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


def _make_fact(
    text: str = "测试事实",
    category: str = "working",
    is_active: bool = True,
    embedding: list[float] | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    access_count: int = 0,
    last_accessed_at: datetime | None = None,
    superseded_by: uuid.UUID | None = None,
    superseded_at: datetime | None = None,
) -> MagicMock:
    """创建模拟 MemoryFact 对象（含遗忘机制新字段）。"""
    fact = MagicMock()
    fact.id = uuid.uuid4()
    fact.fact_text = text
    fact.category = category
    fact.fact_key = None
    fact.fact_value = None
    fact.is_active = is_active
    fact.embedding = embedding
    fact.created_at = created_at or datetime.utcnow()
    fact.expires_at = expires_at
    fact.access_count = access_count
    fact.last_accessed_at = last_accessed_at
    fact.superseded_by = superseded_by
    fact.superseded_at = superseded_at
    fact.user_id = uuid.uuid4()
    return fact


# ======================================================================
# 机制一：ACT-R 三因子激活值
# ======================================================================


class TestActivationACTR:
    """forgetting.DefaultActivation 三因子计算。"""

    def _policy(self, **kwargs) -> "DefaultActivation":
        from app.memory.forgetting import DefaultActivation

        return DefaultActivation(
            freq_weight=kwargs.get("freq_weight", 0.2),
            recency_window_days=kwargs.get("recency_window_days", 7),
            recency_boost=kwargs.get("recency_boost", 0.3),
        )

    def test_preference_without_ttl_always_full(self) -> None:
        """偏好无 TTL：时间不衰减，永远 1.0 上场（遗忘只走冲突路径）。"""
        policy = self._policy()
        old_pref = _make_fact(
            category="preference",
            created_at=datetime.utcnow() - timedelta(days=3650),
            expires_at=None,
        )
        assert policy.activation(old_pref, datetime.utcnow()) == 1.0

    def test_fact_without_ttl_full(self) -> None:
        """无 TTL 事实不参与时间衰减。"""
        policy = self._policy()
        fact = _make_fact(
            category="fact",
            created_at=datetime.utcnow() - timedelta(days=800),
            expires_at=None,
        )
        assert policy.activation(fact, datetime.utcnow()) == 1.0

    def test_fresh_episodic_near_full(self) -> None:
        """新情节记忆激活值接近 1.0。"""
        policy = self._policy()
        now = datetime.utcnow()
        fact = _make_fact(
            category="working",
            created_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=23),  # TTL 24h
        )
        activation = policy.activation(fact, now)
        assert activation > 0.9

    def test_old_episodic_decays(self) -> None:
        """远超 TTL 的情节记忆被时间衰减压低。"""
        policy = self._policy()
        now = datetime.utcnow()
        fact = _make_fact(
            category="detail",
            created_at=now - timedelta(days=30),  # detail TTL 72h，早已过期尺度
            expires_at=now + timedelta(hours=72) - timedelta(days=30),
        )
        activation = policy.activation(fact, now)
        assert activation < 0.5

    def test_frequency_gain_rescues_old_memory(self) -> None:
        """天天用的记忆不忘：高访问次数把老记忆的激活值拉回来。"""
        policy = self._policy(freq_weight=0.2)
        now = datetime.utcnow()
        created = now - timedelta(days=30)
        cold = _make_fact(
            category="working",
            created_at=created,
            expires_at=created + timedelta(hours=24),
            access_count=0,
        )
        hot = _make_fact(
            category="working",
            created_at=created,
            expires_at=created + timedelta(hours=24),
            access_count=500,
        )
        cold_act = policy.activation(cold, now)
        hot_act = policy.activation(hot, now)
        assert hot_act > cold_act
        assert hot_act > 0.5  # log(501)*0.2 ≈ 1.24 → 封顶 1.0

    def test_recency_gain_within_window(self) -> None:
        """近期增益：7 天内被召回过额外续命。"""
        policy = self._policy(recency_boost=0.3, recency_window_days=7)
        now = datetime.utcnow()
        base = _make_fact(
            category="working",
            created_at=now - timedelta(days=20),
            expires_at=now - timedelta(days=19) + timedelta(hours=24),
        )
        recent = _make_fact(
            category="working",
            created_at=base.created_at,
            expires_at=base.expires_at,
            last_accessed_at=now - timedelta(days=1),
        )
        assert policy.activation(recent, now) > policy.activation(base, now)

    def test_recency_zero_after_window(self) -> None:
        """超出近期窗口后增益归零。"""
        policy = self._policy(recency_boost=0.3, recency_window_days=7)
        now = datetime.utcnow()
        fact = _make_fact(
            category="working",
            created_at=now - timedelta(days=30),
            expires_at=now - timedelta(days=29) + timedelta(hours=24),
            last_accessed_at=now - timedelta(days=10),
        )
        # 纯时间衰减
        import math

        expected = math.exp(-30.0 / 1.0)
        assert policy.activation(fact, now) == pytest.approx(expected, abs=1e-6)

    def test_activation_capped_at_one(self) -> None:
        """三因子之和封顶 1.0。"""
        policy = self._policy()
        now = datetime.utcnow()
        fact = _make_fact(
            category="working",
            created_at=now,
            expires_at=now + timedelta(hours=24),
            access_count=1000,
            last_accessed_at=now,
        )
        assert policy.activation(fact, now) == 1.0


# ======================================================================
# 机制一：召回闸门与命中写回
# ======================================================================


class TestRankCandidatesGate:
    """_rank_candidates 激活值闸门 + 复活窗口。"""

    def _manager(self) -> "Mem0Manager":
        from app.memory.mem0_manager import Mem0Manager

        return Mem0Manager(MagicMock())

    def test_low_activation_filtered(self, monkeypatch) -> None:
        """激活值低于地板值的记忆当场跳过。"""
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_ENABLED", True, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_FLOOR", 0.5, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_REVIVAL_THRESHOLD", 0.9, raising=False
        )
        manager = self._manager()
        now = datetime.utcnow()
        stale = _make_fact(
            category="working",
            created_at=now - timedelta(days=90),
            expires_at=now - timedelta(days=89) + timedelta(hours=24),
        )  # 激活值极低
        fresh = _make_fact(category="preference")  # 恒 1.0

        results, revived = manager._rank_candidates(
            [(0.9, stale), (0.8, fresh)]
        )
        assert results == [fresh]
        assert revived == []

    def test_score_is_sim_times_activation(self, monkeypatch) -> None:
        """排序分 = 相似度 × 激活值：低相似新记忆可胜高相似老记忆。"""
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_ENABLED", True, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_FLOOR", 0.0, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_REVIVAL_THRESHOLD", 0.9, raising=False
        )
        manager = self._manager()
        now = datetime.utcnow()
        old_high_sim = _make_fact(
            category="working",
            created_at=now - timedelta(days=60),
            expires_at=now - timedelta(days=59) + timedelta(hours=24),
        )  # sim 0.95 × 低激活
        new_low_sim = _make_fact(
            category="preference"
        )  # sim 0.7 × 1.0
        results, _ = manager._rank_candidates(
            [(0.95, old_high_sim), (0.7, new_low_sim)]
        )
        assert results[0] is new_low_sim

    def test_superseded_revived_on_strong_hit(self, monkeypatch) -> None:
        """复活窗口：被 superseded 的记忆被强命中（>= 复活阈值）自动复活。"""
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_ENABLED", True, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_FLOOR", 0.05, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_REVIVAL_THRESHOLD", 0.9, raising=False
        )
        manager = self._manager()
        superseded = _make_fact(
            category="preference",
            is_active=False,
            superseded_by=uuid.uuid4(),
            superseded_at=datetime.utcnow() - timedelta(days=1),
        )
        results, revived = manager._rank_candidates([(0.95, superseded)])
        assert results == [superseded]
        assert revived == [superseded]
        assert superseded.is_active is True
        assert superseded.superseded_by is None
        assert superseded.superseded_at is None

    def test_superseded_weak_hit_stays_out(self, monkeypatch) -> None:
        """复活窗口：弱命中（< 复活阈值）继续挡在门外（防误复活）。"""
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_ENABLED", True, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_FLOOR", 0.05, raising=False
        )
        monkeypatch.setattr(
            get_settings(), "MEMORY_REVIVAL_THRESHOLD", 0.9, raising=False
        )
        manager = self._manager()
        superseded = _make_fact(
            category="preference",
            is_active=False,
            superseded_by=uuid.uuid4(),
            superseded_at=datetime.utcnow() - timedelta(days=1),
        )
        results, revived = manager._rank_candidates([(0.7, superseded)])
        assert results == []
        assert revived == []
        assert superseded.is_active is False

    def test_gate_disabled_falls_back_to_similarity(self, monkeypatch) -> None:
        """闸门总开关关闭：退回纯相似度排序（零行为回归开关）。"""
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "MEMORY_ACTIVATION_ENABLED", False, raising=False
        )
        manager = self._manager()
        a = _make_fact(category="working")
        b = _make_fact(category="preference")
        results, revived = manager._rank_candidates([(0.6, a), (0.9, b)])
        assert results == [b, a]
        assert revived == []


class TestRecordAccess:
    """召回命中写回 — 激活值频率/近期因子的数据源。"""

    def _manager(self) -> "Mem0Manager":
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.flush = AsyncMock()
        return Mem0Manager(mock_db)

    @pytest.mark.asyncio
    async def test_access_count_and_recency_written_back(self) -> None:
        """命中后访问次数 +1、最近访问刷新。"""
        manager = self._manager()
        fact = _make_fact(category="fact", access_count=3)
        before = datetime.utcnow()
        await manager._record_access([fact])
        assert fact.access_count == 4
        assert fact.last_accessed_at is not None
        assert fact.last_accessed_at >= before

    @pytest.mark.asyncio
    async def test_preference_rolling_ttl_extended(self) -> None:
        """偏好滚动续命：被召回即延期（天天用的偏好十年不忘）。"""
        manager = self._manager()
        pref = _make_fact(
            category="preference",
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        await manager._record_access([pref])
        assert pref.expires_at > datetime.utcnow() + timedelta(days=89)

    @pytest.mark.asyncio
    async def test_non_preference_ttl_untouched(self) -> None:
        """非偏好记忆的 TTL 不被写回延长。"""
        manager = self._manager()
        working = _make_fact(
            category="working", expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        await manager._record_access([working])
        assert working.expires_at <= datetime.utcnow() + timedelta(hours=2)


class TestMarkSuperseded:
    """冲突整合败者标记。"""

    @pytest.mark.asyncio
    async def test_mark_superseded_sets_markers(self) -> None:
        """败者：is_active=False + superseded_by/at 回填。"""
        from app.memory.mem0_manager import Mem0Manager

        old1 = _make_fact(category="preference", text="喜欢VIP权益")
        old2 = _make_fact(category="preference", text="偏好详细回答")
        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.__iter__.return_value = iter([old1, old2])
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        manager = Mem0Manager(mock_db)
        new_id = uuid.uuid4()
        count = await manager.mark_superseded([old1.id, old2.id], new_id)

        assert count == 2
        for old in (old1, old2):
            assert old.is_active is False
            assert old.superseded_by == new_id
            assert old.superseded_at is not None

    @pytest.mark.asyncio
    async def test_mark_superseded_empty_ids(self) -> None:
        """空列表直接返回 0。"""
        from app.memory.mem0_manager import Mem0Manager

        manager = Mem0Manager(MagicMock())
        assert await manager.mark_superseded([], uuid.uuid4()) == 0


# ======================================================================
# 机制二：写入时增量冲突整合
# ======================================================================


class TestConflictArbiter:
    """MemoryConflictArbiter 裁决分层。"""

    def _arbiter(self, monkeypatch, *, llm_enabled=True, consolidation=True):
        from app.config import get_settings
        from app.memory.conflict_arbiter import MemoryConflictArbiter

        monkeypatch.setattr(
            get_settings(),
            "MEMORY_CONSOLIDATION_ENABLED",
            consolidation,
            raising=False,
        )
        monkeypatch.setattr(
            get_settings(),
            "MEMORY_CONSOLIDATION_LLM_ENABLED",
            llm_enabled,
            raising=False,
        )
        monkeypatch.setattr(
            get_settings(),
            "MEMORY_CONSOLIDATION_TOP_K",
            10,
            raising=False,
        )
        monkeypatch.setattr(
            get_settings(),
            "MEMORY_CONSOLIDATION_CONFLICT_FLOOR",
            0.55,
            raising=False,
        )
        monkeypatch.setattr(
            get_settings(),
            "MEMORY_CONSOLIDATION_DUPLICATE_THRESHOLD",
            0.90,
            raising=False,
        )
        mem0 = MagicMock()
        arbiter = MemoryConflictArbiter(mem0)
        return arbiter, mem0

    @pytest.mark.asyncio
    async def test_no_candidates_write(self, monkeypatch) -> None:
        """无相似候选：语义无关，正常写入。"""
        arbiter, mem0 = self._arbiter(monkeypatch)
        mem0.search_similar_with_scores = AsyncMock(return_value=[])

        verdict = await arbiter.consolidate(uuid.uuid4(), "新事实", "fact")
        assert verdict.action == "write"
        assert verdict.superseded_ids == []

    @pytest.mark.asyncio
    async def test_equivalent_shortcircuit_discards(self, monkeypatch) -> None:
        """相似度 >= 上限：规则短路判等价，新记忆丢弃（省 LLM）。"""
        arbiter, mem0 = self._arbiter(monkeypatch)
        existing = _make_fact(category="fact", text="项目使用 PostgreSQL")
        mem0.search_similar_with_scores = AsyncMock(
            return_value=[(existing, 0.95)]
        )

        verdict = await arbiter.consolidate(uuid.uuid4(), "项目使用 PostgreSQL", "fact")
        assert verdict.action == "discard"
        assert "shortcircuit" in verdict.reason

    @pytest.mark.asyncio
    async def test_llm_conflict_supersedes_old(self, monkeypatch) -> None:
        """LLM 裁决冲突：新胜旧退场。"""
        arbiter, mem0 = self._arbiter(monkeypatch)
        old_pref = _make_fact(category="preference", text="喜欢VIP免费洗车权益")
        mem0.search_similar_with_scores = AsyncMock(
            return_value=[(old_pref, 0.7)]
        )
        arbiter._llm_arbitrate = AsyncMock(
            return_value={old_pref.id: "conflict"}
        )

        verdict = await arbiter.consolidate(
            uuid.uuid4(), "因成本控制将套餐降级为基础版", "fact"
        )
        assert verdict.action == "write"
        assert verdict.superseded_ids == [old_pref.id]

    @pytest.mark.asyncio
    async def test_llm_equivalent_discards(self, monkeypatch) -> None:
        """LLM 裁决等价：旧胜新丢弃（去重）。"""
        arbiter, mem0 = self._arbiter(monkeypatch)
        existing = _make_fact(category="fact", text="用户偏好简洁回答")
        mem0.search_similar_with_scores = AsyncMock(
            return_value=[(existing, 0.7)]
        )
        arbiter._llm_arbitrate = AsyncMock(
            return_value={existing.id: "equivalent"}
        )

        verdict = await arbiter.consolidate(uuid.uuid4(), "用户喜欢简洁回答", "fact")
        assert verdict.action == "discard"

    @pytest.mark.asyncio
    async def test_llm_failure_fail_open(self, monkeypatch) -> None:
        """LLM 失败：fail-open 正常写入，不断言冲突。"""
        arbiter, mem0 = self._arbiter(monkeypatch)
        existing = _make_fact(category="preference", text="旧偏好")
        mem0.search_similar_with_scores = AsyncMock(
            return_value=[(existing, 0.7)]
        )
        arbiter._llm_arbitrate = AsyncMock(return_value={})

        verdict = await arbiter.consolidate(uuid.uuid4(), "新记忆", "fact")
        assert verdict.action == "write"
        assert verdict.superseded_ids == []

    @pytest.mark.asyncio
    async def test_llm_disabled_write_in_band(self, monkeypatch) -> None:
        """LLM 裁决关闭：冲突区间内不裁决，直接写入（降级为纯规则）。"""
        arbiter, mem0 = self._arbiter(monkeypatch, llm_enabled=False)
        existing = _make_fact(category="preference", text="旧偏好")
        mem0.search_similar_with_scores = AsyncMock(
            return_value=[(existing, 0.7)]
        )

        verdict = await arbiter.consolidate(uuid.uuid4(), "新记忆", "fact")
        assert verdict.action == "write"
        assert verdict.superseded_ids == []

    @pytest.mark.asyncio
    async def test_consolidation_disabled(self, monkeypatch) -> None:
        """整合总开关关闭：零行为回归。"""
        arbiter, mem0 = self._arbiter(monkeypatch, consolidation=False)
        mem0.search_similar_with_scores = AsyncMock()

        verdict = await arbiter.consolidate(uuid.uuid4(), "新记忆", "fact")
        assert verdict.action == "write"
        mem0.search_similar_with_scores.assert_not_awaited()

    def test_parse_verdicts_plain_json(self, monkeypatch) -> None:
        """解析纯 JSON 裁决输出。"""
        arbiter, _ = self._arbiter(monkeypatch)
        f1 = _make_fact()
        f2 = _make_fact()
        candidates = [(f1, 0.8), (f2, 0.6)]
        verdicts = arbiter._parse_verdicts(
            '{"1": "conflict", "2": "unrelated"}', candidates
        )
        assert verdicts == {f1.id: "conflict", f2.id: "unrelated"}

    def test_parse_verdicts_markdown_wrapped(self, monkeypatch) -> None:
        """解析 markdown 代码块包裹的 JSON。"""
        arbiter, _ = self._arbiter(monkeypatch)
        f1 = _make_fact()
        verdicts = arbiter._parse_verdicts(
            '```json\n{"1": "equivalent"}\n```', [(f1, 0.8)]
        )
        assert verdicts == {f1.id: "equivalent"}

    def test_parse_verdicts_invalid_returns_none(self, monkeypatch) -> None:
        """解析失败返回 None（上层 fail-open）。"""
        arbiter, _ = self._arbiter(monkeypatch)
        assert arbiter._parse_verdicts("不是JSON", [(_make_fact(), 0.8)]) is None


class TestConsolidatedAdd:
    """memory_manager._consolidated_add 写入编排。"""

    def _memory_manager(self):
        from app.memory.memory_manager import MemoryManager

        return MemoryManager(MagicMock())

    @pytest.mark.asyncio
    async def test_write_with_supersede(self) -> None:
        """裁决 write + 冲突败者：落盘新记忆并回填退场标记。"""
        from app.memory.conflict_arbiter import ACTION_WRITE, ConsolidateVerdict

        mm = self._memory_manager()
        old_id = uuid.uuid4()
        verdict = ConsolidateVerdict(
            ACTION_WRITE, superseded_ids=[old_id], reason="llm_conflict"
        )
        mm.arbiter = MagicMock()
        mm.arbiter.consolidate = AsyncMock(return_value=verdict)
        new_fact = MagicMock()
        new_fact.id = uuid.uuid4()
        mm.mem0.add_fact = AsyncMock(return_value=new_fact)
        mm.mem0.mark_superseded = AsyncMock(return_value=1)

        result = await mm._consolidated_add(uuid.uuid4(), "新事实", "fact")

        assert result is new_fact
        mm.mem0.mark_superseded.assert_awaited_once_with([old_id], new_fact.id)

    @pytest.mark.asyncio
    async def test_discard_skips_write(self) -> None:
        """裁决 discard：不落盘。"""
        from app.memory.conflict_arbiter import ACTION_DISCARD, ConsolidateVerdict

        mm = self._memory_manager()
        mm.arbiter = MagicMock()
        mm.arbiter.consolidate = AsyncMock(
            return_value=ConsolidateVerdict(ACTION_DISCARD, reason="equivalent")
        )
        mm.mem0.add_fact = AsyncMock()

        result = await mm._consolidated_add(uuid.uuid4(), "重复事实", "fact")

        assert result is None
        mm.mem0.add_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_arbiter_crash_falls_back_to_dedup(self) -> None:
        """仲裁器异常：降级为规则判重，fail-open 不阻塞。"""
        mm = self._memory_manager()
        mm.arbiter = MagicMock()
        mm.arbiter.consolidate = AsyncMock(side_effect=RuntimeError("boom"))
        mm._check_duplicate = AsyncMock(return_value=False)
        new_fact = MagicMock()
        mm.mem0.add_fact = AsyncMock(return_value=new_fact)

        result = await mm._consolidated_add(uuid.uuid4(), "新事实", "fact")

        assert result is new_fact
        mm._check_duplicate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_arbiter_crash_duplicate_discarded(self) -> None:
        """仲裁器异常且判重命中：丢弃（保守去重）。"""
        mm = self._memory_manager()
        mm.arbiter = MagicMock()
        mm.arbiter.consolidate = AsyncMock(side_effect=RuntimeError("boom"))
        mm._check_duplicate = AsyncMock(return_value=True)
        mm.mem0.add_fact = AsyncMock()

        result = await mm._consolidated_add(uuid.uuid4(), "重复事实", "fact")

        assert result is None
        mm.mem0.add_fact.assert_not_awaited()


# ======================================================================
# 套餐灾难闭环（文章开篇场景端到端）
# ======================================================================


class TestPlanDowngradeScenario:
    """文章开篇场景：VIP 偏好被降级情节冲突覆写，召回不再平权共存。"""

    @pytest.mark.asyncio
    async def test_full_loop(self, monkeypatch) -> None:
        """降级情节写入 → VIP 偏好退场 → 召回闸门跳过退场记忆。"""
        from app.config import get_settings
        from app.memory.conflict_arbiter import ACTION_WRITE, ConsolidateVerdict
        from app.memory.mem0_manager import Mem0Manager

        settings = get_settings()
        monkeypatch.setattr(settings, "MEMORY_ACTIVATION_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "MEMORY_ACTIVATION_FLOOR", 0.05, raising=False)
        monkeypatch.setattr(
            settings, "MEMORY_REVIVAL_THRESHOLD", 0.9, raising=False
        )
        monkeypatch.setattr(
            settings, "MEMORY_REVIVAL_WINDOW_DAYS", 7, raising=False
        )

        # 1. 写入路径：裁决冲突，新情节落盘，旧偏好退场
        mm = MagicMock()
        vip_pref = _make_fact(
            category="preference", text="喜欢VIP免费洗车权益"
        )
        verdict = ConsolidateVerdict(
            ACTION_WRITE, superseded_ids=[vip_pref.id], reason="llm_conflict"
        )
        mm.arbiter.consolidate = AsyncMock(return_value=verdict)
        new_fact = MagicMock()
        new_fact.id = uuid.uuid4()
        mm.mem0.add_fact = AsyncMock(return_value=new_fact)
        mm.mem0.mark_superseded = AsyncMock(return_value=1)
        from app.memory.memory_manager import MemoryManager

        written = await MemoryManager._consolidated_add(
            mm, uuid.uuid4(), "因成本控制将套餐降级为基础版", "fact"
        )
        assert written is new_fact

        # 2. mark_superseded 实际打标
        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.__iter__.return_value = iter([vip_pref])
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mem0 = Mem0Manager(mock_db)
        await mem0.mark_superseded([vip_pref.id], new_fact.id)
        assert vip_pref.is_active is False
        assert vip_pref.superseded_by == new_fact.id

        # 3. 召回闸门：退场记忆弱命中不上场（强命中才会复活）
        _, revived = mem0._rank_candidates([(0.75, vip_pref)])
        assert revived == []
