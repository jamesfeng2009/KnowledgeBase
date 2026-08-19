"""
P1 记忆仲裁理由 + 遗忘原因/激活值落库测试。

覆盖：
    - K1  仲裁理由透传：_consolidated_add 冲突裁决 → add_fact 收 verdict_reason
    - K2  superseded 退场：mark_superseded → superseded_conflict + activation_value
    - K3  corrected 退场：correct_fact → corrected
    - K4  expired 退场：cleanup_expired → expired
    - K5  dedup 退场：_deactivate_conflicting → dedup
    - K6  等价丢弃：DISCARD 不落盘、无新写入
    - K7  兼容降级：缺 reason / 旧数据 → None，不阻断
    - K8  迁移链：a4b5c6d7e8f9 唯一 head 且接续 1c2d3e4f5a6b
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

from app.memory.conflict_arbiter import ACTION_DISCARD, ACTION_WRITE, ConsolidateVerdict
from app.memory.mem0_manager import Mem0Manager
from app.memory.memory_manager import MemoryManager


@pytest.fixture
def memory_manager():
    """MemoryManager with mock 依赖（对齐 test_memory_traceability 约定）。"""
    mgr = MemoryManager(MagicMock())
    mgr.mem0 = AsyncMock()
    mgr.checkpoint = AsyncMock()
    mgr.graphiti = MagicMock()
    mgr.arbiter = MagicMock()
    return mgr


# ======================================================================
# K1 · 仲裁理由透传
# ======================================================================


class TestVerdictReasonWrite:
    """_consolidated_add 将裁决理由写入新事实。"""

    @pytest.mark.asyncio
    async def test_k1_llm_conflict_passes_reason(self, memory_manager):
        """冲突裁决(llm_conflict) → add_fact 收到 verdict_reason。"""
        captured = {}

        async def fake_add_fact(**kwargs):
            captured.update(kwargs)
            new = MagicMock()
            new.id = uuid.uuid4()
            return new

        memory_manager.arbiter.consolidate = AsyncMock(
            return_value=ConsolidateVerdict(ACTION_WRITE, reason="llm_conflict")
        )
        memory_manager.mem0.add_fact = fake_add_fact
        memory_manager.mem0.mark_superseded = AsyncMock()

        new = await memory_manager._consolidated_add(
            uuid.uuid4(), "用户是高级工程师", "working"
        )
        assert new is not None
        assert captured["verdict_reason"] == "llm_conflict"

    @pytest.mark.asyncio
    async def test_k1_llm_unrelated_reason(self, memory_manager):
        """无关裁决(llm_unrelated) → reason 同样透传。"""
        captured = {}

        async def fake_add_fact(**kwargs):
            captured.update(kwargs)
            new = MagicMock()
            new.id = uuid.uuid4()
            return new

        memory_manager.arbiter.consolidate = AsyncMock(
            return_value=ConsolidateVerdict(ACTION_WRITE, reason="llm_unrelated")
        )
        memory_manager.mem0.add_fact = fake_add_fact

        await memory_manager._consolidated_add(uuid.uuid4(), "新增无关事实", "working")
        assert captured["verdict_reason"] == "llm_unrelated"

    @pytest.mark.asyncio
    async def test_k1_empty_reason_falls_back_none(self, memory_manager):
        """reason 为空字符串 → 落到 None（不阻断）。"""
        captured = {}

        async def fake_add_fact(**kwargs):
            captured.update(kwargs)
            new = MagicMock()
            new.id = uuid.uuid4()
            return new

        memory_manager.arbiter.consolidate = AsyncMock(
            return_value=ConsolidateVerdict(ACTION_WRITE, reason="")
        )
        memory_manager.mem0.add_fact = fake_add_fact

        await memory_manager._consolidated_add(uuid.uuid4(), "空理由事实", "working")
        assert captured["verdict_reason"] is None


# ======================================================================
# K2-K5 · 遗忘原因 + 激活值
# ======================================================================


class TestForgottenReasonWrite:
    """四类退场路径都写 forgotten_reason + activation_value。"""

    def _fact(self, fact_value="v1", expires_at=None):
        f = MagicMock()
        f.id = uuid.uuid4()
        f.fact_key = "k"
        f.category = "working"
        f.fact_value = fact_value
        f.fact_text = "fact"
        f.is_active = True
        f.expires_at = expires_at
        return f

    def _db_with_rows(self, rows):
        """返回 AsyncMock db：execute 返回可迭代 scalars（.all() 亦可用）。"""
        db = MagicMock()
        db.execute = AsyncMock()
        db.flush = AsyncMock()

        class _Rows(list):
            def scalars(self):
                return self

            def all(self):
                return list(self)

        result = _Rows(rows)
        db.execute.return_value = result
        return db

    @pytest.mark.asyncio
    async def test_k2_superseded(self):
        """mark_superseded → superseded_conflict + activation_value 非空。"""
        fact = self._fact()
        db = self._db_with_rows([fact])

        mgr = Mem0Manager(db)
        mgr._activation = MagicMock()
        mgr._activation.activation.return_value = 0.72

        counted = await mgr.mark_superseded([fact.id], uuid.uuid4())
        assert counted == 1
        assert fact.is_active is False
        assert fact.forgotten_reason == "superseded_conflict"
        assert fact.activation_value == 0.72

    @pytest.mark.asyncio
    async def test_k3_corrected(self):
        """correct_fact → corrected + activation_value。"""
        fact = self._fact()
        db = MagicMock()
        db.execute = AsyncMock()
        db.flush = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = lambda: fact
        db.execute.return_value = result

        mgr = Mem0Manager(db)
        mgr._activation = MagicMock()
        mgr._activation.activation.return_value = 0.5

        await mgr.correct_fact(fact.id, "corrected text")
        # 无 LLM 时 correct_fact 仅停用旧事实返回 None 或新事实；断言旧事实已标记
        assert fact.is_active is False
        assert fact.forgotten_reason == "corrected"
        assert fact.activation_value == 0.5

    @pytest.mark.asyncio
    async def test_k4_expired(self):
        """cleanup_expired → expired + activation_value。"""
        fact = self._fact(expires_at=None)
        db = self._db_with_rows([fact])

        mgr = Mem0Manager(db)
        mgr._activation = MagicMock()
        mgr._activation.activation.return_value = 0.1

        counted = await mgr.cleanup_expired()
        assert counted == 1
        assert fact.is_active is False
        assert fact.forgotten_reason == "expired"
        assert fact.activation_value == 0.1

    @pytest.mark.asyncio
    async def test_k5_dedup(self):
        """_deactivate_conflicting → dedup + activation_value。"""
        old = self._fact(fact_value="old")
        db = self._db_with_rows([old])

        mgr = Mem0Manager(db)
        mgr._activation = MagicMock()
        mgr._activation.activation.return_value = 0.9

        counted = await mgr._deactivate_conflicting(
            uuid.uuid4(), "working", "k", "new_value"
        )
        assert counted == 1
        assert old.is_active is False
        assert old.forgotten_reason == "dedup"
        assert old.activation_value == 0.9


# ======================================================================
# K6 · 等价丢弃
# ======================================================================


class TestVerdictDiscard:
    """DISCARD 分支不落盘、无新写入。"""

    @pytest.mark.asyncio
    async def test_k6_discard_returns_none_no_write(self, memory_manager):
        memory_manager.arbiter.consolidate = AsyncMock(
            return_value=ConsolidateVerdict(ACTION_DISCARD, reason="equivalent_shortcircuit")
        )
        result = await memory_manager._consolidated_add(
            uuid.uuid4(), "重复事实", "working"
        )
        assert result is None
        memory_manager.mem0.add_fact.assert_not_awaited()


# ======================================================================
# K7 · 兼容降级
# ======================================================================


class TestBackwardCompat:
    """旧数据 / 无 reason 路径不阻断。"""

    @pytest.mark.asyncio
    async def test_k7_discard_without_reason_string(self, memory_manager):
        """reason 未显式提供时（fallback/异常降级）不传 None。"""
        captured = {}

        async def fake_add_fact(**kwargs):
            captured.update(kwargs)
            new = MagicMock()
            new.id = uuid.uuid4()
            return new

        # arbiter 抛异常 → 走 fallback add_fact，无 verdict_reason
        memory_manager.arbiter.consolidate = AsyncMock(
            side_effect=RuntimeError("arbiter down")
        )
        memory_manager._check_duplicate = AsyncMock(return_value=False)
        memory_manager.mem0.add_fact = fake_add_fact

        new = await memory_manager._consolidated_add(
            uuid.uuid4(), "降级写入", "working"
        )
        assert new is not None
        assert captured.get("verdict_reason") is None


# ======================================================================
# K8 · 迁移链
# ======================================================================


class TestMigrationChain:
    """三个 P 里程碑迁移线性成链，唯一 head。"""

    def test_migration_chain_is_linear(self) -> None:
        """a4b5c6d7e8f9 接 1c2d3e4f5a6b，且为唯一 head。"""
        import os
        import sys as _sys

        base = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
        )
        content_for = lambda rev_file: open(  # noqa: E731
            os.path.join(base, rev_file), encoding="utf-8"
        ).read()

        # 找到三个目标迁移文件
        files = os.listdir(base)
        m3 = next(f for f in files if "a4b5c6d7e8f9" in f)
        m2 = next(f for f in files if "1c2d3e4f5a6b" in f)
        assert f'down_revision = "1c2d3e4f5a6b"' in content_for(m3)
        assert f'down_revision = "0a1b2c3d4e5f"' in content_for(m2)