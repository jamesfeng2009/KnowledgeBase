"""
Mem0 冲突检测与用户纠错测试 — app/memory/mem0_manager.py。

覆盖范围：
    - _deactivate_conflicting 冲突检测（同 key 不同 value 自动停用旧项）
    - add_fact 集成冲突检测
    - correct_fact 用户纠错入口
    - set_preference 利用内置冲突检测
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
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
    category: str = "preference",
    fact_key: str | None = None,
    fact_value: str | None = None,
    is_active: bool = True,
    embedding: list[float] | None = None,
) -> MagicMock:
    """创建模拟 MemoryFact 对象。"""
    fact = MagicMock()
    fact.id = uuid.uuid4()
    fact.fact_text = text
    fact.category = category
    fact.fact_key = fact_key
    fact.fact_value = fact_value
    fact.is_active = is_active
    fact.embedding = embedding
    fact.created_at = datetime.utcnow()
    fact.expires_at = None
    fact.user_id = uuid.uuid4()
    return fact


def _mock_db_with_facts(facts: list[MagicMock]) -> MagicMock:
    """创建返回指定 facts 列表的 mock db。"""
    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = facts
    mock_db.execute.return_value = mock_result
    mock_db.flush = AsyncMock()
    return mock_db


# ======================================================================
# _deactivate_conflicting 冲突检测测试
# ======================================================================


class TestDeactivateConflicting:
    """_deactivate_conflicting 冲突检测测试。"""

    @pytest.mark.asyncio
    async def test_no_key_returns_zero(self) -> None:
        """无 fact_key 时不触发冲突检测。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        manager = Mem0Manager(mock_db)

        result = await manager._deactivate_conflicting(
            user_id=uuid.uuid4(),
            category="preference",
            fact_key=None,
            fact_value="value",
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_no_value_returns_zero(self) -> None:
        """无 fact_value 时不触发冲突检测。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        manager = Mem0Manager(mock_db)

        result = await manager._deactivate_conflicting(
            user_id=uuid.uuid4(),
            category="preference",
            fact_key="style",
            fact_value=None,
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_deactivates_conflicting_facts(self) -> None:
        """同 key 不同 value 的旧事实被自动停用。"""
        from app.memory.mem0_manager import Mem0Manager

        old_fact = _make_fact(
            text="style: detailed",
            fact_key="style",
            fact_value="detailed",
        )
        mock_db = _mock_db_with_facts([old_fact])
        manager = Mem0Manager(mock_db)

        result = await manager._deactivate_conflicting(
            user_id=uuid.uuid4(),
            category="preference",
            fact_key="style",
            fact_value="concise",
        )

        assert result == 1
        assert old_fact.is_active is False

    @pytest.mark.asyncio
    async def test_same_value_no_conflict(self) -> None:
        """同 key 同 value 不算冲突，不停用。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = _mock_db_with_facts([])
        manager = Mem0Manager(mock_db)

        result = await manager._deactivate_conflicting(
            user_id=uuid.uuid4(),
            category="preference",
            fact_key="style",
            fact_value="concise",
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_multiple_conflicts_deactivated(self) -> None:
        """多条冲突旧事实全部停用。"""
        from app.memory.mem0_manager import Mem0Manager

        old1 = _make_fact(text="style: detailed", fact_key="style", fact_value="detailed")
        old2 = _make_fact(text="style: verbose", fact_key="style", fact_value="verbose")
        mock_db = _mock_db_with_facts([old1, old2])
        manager = Mem0Manager(mock_db)

        result = await manager._deactivate_conflicting(
            user_id=uuid.uuid4(),
            category="preference",
            fact_key="style",
            fact_value="concise",
        )

        assert result == 2
        assert old1.is_active is False
        assert old2.is_active is False


# ======================================================================
# add_fact 集成冲突检测测试
# ======================================================================


class TestAddFactWithConflictDetection:
    """add_fact 集成冲突检测测试。"""

    @pytest.mark.asyncio
    async def test_add_fact_triggers_conflict_detection(self) -> None:
        """add_fact 在写入前执行冲突检测。"""
        from app.memory.mem0_manager import Mem0Manager

        old_fact = _make_fact(
            text="style: detailed",
            fact_key="style",
            fact_value="detailed",
        )

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_fact]
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock()

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[0.1, 0.2]])

        new_fact = await manager.add_fact(
            user_id=uuid.uuid4(),
            fact_text="style: concise",
            category="preference",
            fact_key="style",
            fact_value="concise",
        )

        # 旧事实被停用
        assert old_fact.is_active is False
        # 新事实被创建
        assert new_fact.fact_value == "concise"

    @pytest.mark.asyncio
    async def test_add_fact_no_conflict_when_no_key(self) -> None:
        """无 fact_key 时不触发冲突检测。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock()

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[0.1, 0.2]])

        new_fact = await manager.add_fact(
            user_id=uuid.uuid4(),
            fact_text="random fact",
            category="working",
        )

        # 没有冲突检测的 DB 查询（execute 不应被用于冲突查询）
        # 新事实正常创建
        assert new_fact is not None


# ======================================================================
# correct_fact 用户纠错测试
# ======================================================================


class TestCorrectFact:
    """correct_fact 用户纠错入口测试。"""

    @pytest.mark.asyncio
    async def test_correct_fact_not_found(self) -> None:
        """纠正不存在的事实返回 None。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        # scalar_one_or_none 返回 None
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        manager = Mem0Manager(mock_db)

        result = await manager.correct_fact(fact_id=uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_correct_fact_deactivate_only(self) -> None:
        """仅停用纠错（不提供纠正文本）。"""
        from app.memory.mem0_manager import Mem0Manager

        old_fact = _make_fact(text="错误的事实")

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = old_fact
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()

        manager = Mem0Manager(mock_db)

        result = await manager.correct_fact(fact_id=old_fact.id)

        assert result is old_fact
        assert old_fact.is_active is False

    @pytest.mark.asyncio
    async def test_correct_fact_with_replacement(self) -> None:
        """纠正并提供替换文本。"""
        from app.memory.mem0_manager import Mem0Manager

        old_fact = _make_fact(
            text="style: detailed",
            category="preference",
            fact_key="style",
            fact_value="detailed",
        )

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = old_fact
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock()

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[0.5, 0.5]])

        result = await manager.correct_fact(
            fact_id=old_fact.id,
            corrected_text="style: concise",
            corrected_value="concise",
        )

        # 旧事实被停用
        assert old_fact.is_active is False
        # 新事实被创建且使用纠正值
        assert result is not old_fact
        assert result.fact_value == "concise"


# ======================================================================
# set_preference 冲突检测集成测试
# ======================================================================


class TestSetPreferenceConflict:
    """set_preference 冲突检测集成测试。"""

    @pytest.mark.asyncio
    async def test_set_preference_auto_deactivates_old(self) -> None:
        """set_preference 通过 add_fact 自动停用冲突旧偏好。"""
        from app.memory.mem0_manager import Mem0Manager

        old_fact = _make_fact(
            text="style: detailed",
            fact_key="style",
            fact_value="detailed",
        )

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_fact]
        mock_db.execute.return_value = mock_result
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock()

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[0.1]])

        new_fact = await manager.set_preference(
            user_id=uuid.uuid4(),
            key="style",
            value="concise",
        )

        # 旧偏好被停用（通过 _deactivate_conflicting）
        assert old_fact.is_active is False
        # 新偏好被创建
        assert new_fact.fact_value == "concise"

    @pytest.mark.asyncio
    async def test_set_preference_same_value_no_conflict(self) -> None:
        """设置相同值的偏好不产生冲突。"""
        from app.memory.mem0_manager import Mem0Manager

        mock_db = _mock_db_with_facts([])
        mock_db.add = MagicMock()

        manager = Mem0Manager(mock_db)
        manager._embedder = MagicMock()
        manager._embedder.embed = AsyncMock(return_value=[[0.1]])

        new_fact = await manager.set_preference(
            user_id=uuid.uuid4(),
            key="style",
            value="concise",
        )

        assert new_fact.fact_value == "concise"
