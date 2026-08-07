"""
偏好变更同步 Graphiti 时序图谱测试 — P2-2。

覆盖范围：
    - GraphitiManager.record_preference_change（首次注册实体 / 复用实体）
    - MemoryManager.set_preference 编排（读旧值 → 写新值 → 时序同步）
    - Graphiti 同步失败不阻断主流程
"""

from __future__ import annotations

import sys
import uuid
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


def _make_db_first_change() -> MagicMock:
    """首次变更场景的 mock db：实体不存在，无历史事件。"""
    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()

    # 第一次 execute：查实体 → None；第二次：查历史事件 → 空；
    # 第三次：record_event 回查实体以更新 current_version（Bug32 修复新增）
    entity_result = MagicMock()
    entity_result.scalar_one_or_none.return_value = None
    prev_events_result = MagicMock()
    prev_events_result.scalars.return_value = []  # record_event 直接迭代 scalars()
    refresh_result = MagicMock()
    refresh_result.scalar_one_or_none.return_value = MagicMock()  # 注册后的实体

    mock_db.execute = AsyncMock(
        side_effect=[entity_result, prev_events_result, refresh_result]
    )
    return mock_db


def _make_db_existing_entity(entity: MagicMock) -> MagicMock:
    """后续变更场景的 mock db：实体已存在，无未完成历史事件。"""
    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()

    entity_result = MagicMock()
    entity_result.scalar_one_or_none.return_value = entity
    prev_events_result = MagicMock()
    prev_events_result.scalars.return_value = []  # record_event 直接迭代 scalars()
    # record_event 回查实体以更新 current_version（Bug32 修复新增）
    refresh_result = MagicMock()
    refresh_result.scalar_one_or_none.return_value = entity

    mock_db.execute = AsyncMock(
        side_effect=[entity_result, prev_events_result, refresh_result]
    )
    return mock_db


# ======================================================================
# GraphitiManager.record_preference_change 测试
# ======================================================================


class TestRecordPreferenceChange:
    """GraphitiManager.record_preference_change 测试。"""

    @pytest.mark.asyncio
    async def test_first_change_registers_entity(self) -> None:
        """首次偏好变更自动注册 user_preference 实体。"""
        from app.memory.graphiti_manager import GraphitiManager

        mock_db = _make_db_first_change()
        manager = GraphitiManager(mock_db)
        user_id = uuid.uuid4()

        event = await manager.record_preference_change(
            user_id=user_id,
            key="answer_style",
            old_value=None,
            new_value="简洁",
        )

        # register_entity + record_event 各 add 一次
        assert mock_db.add.call_count == 2
        # 注册实体名为 user_pref:{user_id}:{key}
        added_entity = mock_db.add.call_args_list[0][0][0]
        assert added_entity.entity_type == "user_preference"
        assert added_entity.name == f"user_pref:{user_id}:answer_style"
        assert added_entity.entity_ref_id == user_id
        # 事件记录旧值 None、新值"简洁"
        assert event.event_type == "preference_changed"
        assert event.old_value is None
        assert event.new_value == "简洁"
        assert event.event_source == "user"

    @pytest.mark.asyncio
    async def test_subsequent_change_reuses_entity(self) -> None:
        """后续变更复用已存在的实体，不重复注册。"""
        from app.memory.graphiti_manager import GraphitiManager

        existing_entity = MagicMock()
        existing_entity.id = uuid.uuid4()

        mock_db = _make_db_existing_entity(existing_entity)
        manager = GraphitiManager(mock_db)

        event = await manager.record_preference_change(
            user_id=uuid.uuid4(),
            key="answer_style",
            old_value="简洁",
            new_value="详细",
        )

        # 只 add 事件，不再注册实体
        assert mock_db.add.call_count == 1
        assert event.entity_id == existing_entity.id
        assert event.old_value == "简洁"
        assert event.new_value == "详细"
        # Bug32 回归：偏好变更后实体 current_version 同步更新为新值
        assert existing_entity.current_version == "详细"

    @pytest.mark.asyncio
    async def test_event_closes_previous_event(self) -> None:
        """新事件关闭上一个未完成事件的 valid_to（时间线连续性）。"""
        from app.memory.graphiti_manager import GraphitiManager

        existing_entity = MagicMock()
        existing_entity.id = uuid.uuid4()

        prev_event = MagicMock()
        prev_event.valid_to = None

        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()
        entity_result = MagicMock()
        entity_result.scalar_one_or_none.return_value = existing_entity
        prev_events_result = MagicMock()
        prev_events_result.scalars.return_value = [prev_event]  # 直接迭代 scalars()
        # record_event 回查实体以更新 current_version（Bug32 修复新增）
        refresh_result = MagicMock()
        refresh_result.scalar_one_or_none.return_value = existing_entity
        mock_db.execute = AsyncMock(
            side_effect=[entity_result, prev_events_result, refresh_result]
        )

        manager = GraphitiManager(mock_db)
        await manager.record_preference_change(
            user_id=uuid.uuid4(),
            key="language",
            old_value="中文",
            new_value="英文",
        )

        # 上一个事件的 valid_to 被关闭
        assert prev_event.valid_to is not None


# ======================================================================
# MemoryManager.set_preference 编排测试
# ======================================================================


class TestMemoryManagerSetPreference:
    """MemoryManager.set_preference 编排测试（Mem0 + Graphiti 协调）。"""

    def _make_manager(
        self,
        old_value: str | None = "简洁",
        graphiti_error: Exception | None = None,
    ) -> MagicMock:
        """创建带 mock 子管理器的 MemoryManager stub。"""
        manager = MagicMock()

        new_fact = MagicMock()
        new_fact.fact_value = "详细"

        manager.mem0.get_preference = AsyncMock(return_value=old_value)
        manager.mem0.set_preference = AsyncMock(return_value=new_fact)

        if graphiti_error:
            manager.graphiti.record_preference_change = AsyncMock(
                side_effect=graphiti_error
            )
        else:
            manager.graphiti.record_preference_change = AsyncMock()

        return manager

    @pytest.mark.asyncio
    async def test_orchestration_flow(self) -> None:
        """编排流程：读旧值 → Mem0 写新值 → Graphiti 记录变更事件。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_manager(old_value="简洁")
        user_id = uuid.uuid4()

        fact = await MemoryManager.set_preference(
            manager,
            user_id=user_id,
            key="answer_style",
            value="详细",
        )

        # Mem0 写入被调用
        manager.mem0.set_preference.assert_awaited_once_with(
            user_id=user_id, key="answer_style", value="详细", fact_text=None
        )
        # Graphiti 同步带正确的旧值/新值
        manager.graphiti.record_preference_change.assert_awaited_once_with(
            user_id=user_id,
            key="answer_style",
            old_value="简洁",
            new_value="详细",
        )
        assert fact.fact_value == "详细"

    @pytest.mark.asyncio
    async def test_first_preference_old_value_none(self) -> None:
        """首次设置偏好时旧值为 None。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_manager(old_value=None)
        user_id = uuid.uuid4()

        await MemoryManager.set_preference(
            manager, user_id=user_id, key="answer_style", value="简洁"
        )

        manager.graphiti.record_preference_change.assert_awaited_once_with(
            user_id=user_id,
            key="answer_style",
            old_value=None,
            new_value="简洁",
        )

    @pytest.mark.asyncio
    async def test_graphiti_failure_not_blocking(self) -> None:
        """Graphiti 同步失败不阻断 Mem0 主流程，仍返回新事实。"""
        from app.memory.memory_manager import MemoryManager

        manager = self._make_manager(graphiti_error=RuntimeError("graph down"))
        user_id = uuid.uuid4()

        fact = await MemoryManager.set_preference(
            manager, user_id=user_id, key="answer_style", value="详细"
        )

        # 主流程结果正常返回
        assert fact.fact_value == "详细"
        manager.mem0.set_preference.assert_awaited_once()
