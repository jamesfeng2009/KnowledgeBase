"""P2 轻量 Outbox 测试 — 一次性触发链路的欠投递记录与定时补投。

覆盖：
    - dispatch_with_outbox：成功派发不落箱 / 派发失败落箱；
    - record_failed_dispatch：独立短事务落箱 / 落箱失败容错；
    - dispatch_with_outbox_sync：同步 Celery 任务版本；
    - flush_task_outbox：pending 补投 → sent / 失败 attempts+1 / 达上限 dead；
    - beat_schedule 注册存在性。
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ==================================================================
# 辅助 — 构造 mock outbox 条目 / DB 会话
# ==================================================================

def _make_entry(
    attempts: int = 0,
    task_name: str = "tasks.compounding_tasks.trigger_chat_feedback_compounding",
    status: str = "pending",
) -> MagicMock:
    entry = MagicMock()
    entry.id = uuid.uuid4()
    entry.task_name = task_name
    entry.task_kwargs = {"feedback_id": "fb-1", "tenant_id": None}
    entry.attempts = attempts
    entry.status = status
    return entry


def _make_task_db_session(entries: list[MagicMock]) -> MagicMock:
    """构造 task_db_session mock。

    SELECT（查询 pending）返回 entries；UPDATE（状态回写）返回普通结果。
    """
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = entries

    async def execute_side_effect(stmt, *args, **kwargs):
        sql_head = str(stmt).strip()[:12].upper()
        if sql_head.startswith("SELECT"):
            return query_result
        return MagicMock()

    def factory():
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=execute_side_effect)
        session.commit = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    return factory


# ==================================================================
# dispatch_with_outbox
# ==================================================================

class TestDispatchWithOutbox:
    """异步派发助手。"""

    @pytest.mark.asyncio
    async def test_success_does_not_record(self) -> None:
        """派发成功 → 返回 True，不写 outbox。"""
        from app.services import task_outbox

        task = MagicMock()
        task.name = "tasks.demo.task"
        task.delay = MagicMock()

        with patch.object(
            task_outbox, "record_failed_dispatch", new=AsyncMock()
        ) as mock_record:
            result = await task_outbox.dispatch_with_outbox(task, doc_id="d1")

        assert result is True
        task.delay.assert_called_once_with(doc_id="d1")
        mock_record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_records_outbox(self) -> None:
        """派发失败（broker 不可用）→ 返回 False，落箱记录欠投递。"""
        from app.services import task_outbox

        task = MagicMock()
        task.name = "tasks.demo.task"
        task.delay = MagicMock(side_effect=Exception("broker down"))

        with patch.object(
            task_outbox, "record_failed_dispatch", new=AsyncMock(return_value=True)
        ) as mock_record:
            result = await task_outbox.dispatch_with_outbox(task, doc_id="d1")

        assert result is False
        mock_record.assert_awaited_once_with(
            "tasks.demo.task", {"doc_id": "d1"}, mock_record.await_args.args[2]
        )


# ==================================================================
# record_failed_dispatch
# ==================================================================

class TestRecordFailedDispatch:
    """独立短事务落箱。"""

    @pytest.mark.asyncio
    async def test_records_pending_row(self) -> None:
        """落箱成功 → INSERT pending + last_error，返回 True。"""
        from app.services import task_outbox

        mock_session = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock(return_value=ctx)

        with patch(
            "app.database.async_session_factory", new=mock_factory
        ):
            result = await task_outbox.record_failed_dispatch(
                "tasks.demo.task", {"doc_id": "d1"}, Exception("broker down")
            )

        assert result is True
        mock_session.execute.assert_awaited_once()
        sql_text = str(mock_session.execute.await_args.args[0])
        assert "INSERT INTO task_outbox" in sql_text
        params = mock_session.execute.await_args.args[1]
        assert params["task_name"] == "tasks.demo.task"
        assert "broker down" in params["last_error"]
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_failure_returns_false_without_raise(self) -> None:
        """DB 也不可用（极端场景）→ 返回 False，不抛异常、不阻断主流程。"""
        from app.services import task_outbox

        mock_factory = MagicMock(side_effect=Exception("db down"))

        with patch("app.database.async_session_factory", new=mock_factory):
            result = await task_outbox.record_failed_dispatch(
                "tasks.demo.task", {"doc_id": "d1"}, Exception("broker down")
            )

        assert result is False


# ==================================================================
# dispatch_with_outbox_sync
# ==================================================================

class TestDispatchWithOutboxSync:
    """同步版本（视频 finalize 等同步 Celery 任务使用）。"""

    def test_success_returns_true(self) -> None:
        from app.services import task_outbox

        task = MagicMock()
        task.name = "tasks.demo.task"
        task.delay = MagicMock()

        with patch.object(
            task_outbox, "record_failed_dispatch", new=AsyncMock()
        ) as mock_record:
            result = task_outbox.dispatch_with_outbox_sync(task, doc_id="d1")

        assert result is True
        mock_record.assert_not_awaited()

    def test_failure_records_outbox_via_asyncio(self) -> None:
        """派发失败 → asyncio.run(record_failed_dispatch) 落箱。"""
        from app.services import task_outbox

        task = MagicMock()
        task.name = "tasks.demo.task"
        task.delay = MagicMock(side_effect=Exception("broker down"))

        with patch.object(
            task_outbox, "record_failed_dispatch", new=AsyncMock(return_value=True)
        ) as mock_record:
            result = task_outbox.dispatch_with_outbox_sync(task, doc_id="d1")

        assert result is False
        mock_record.assert_awaited_once()
        assert mock_record.await_args.args[0] == "tasks.demo.task"


# ==================================================================
# flush_task_outbox — 定时补投
# ==================================================================

class TestFlushTaskOutbox:
    """Celery 定时任务 flush_task_outbox。"""

    def test_celery_entry_runs(self) -> None:
        """Celery 任务入口正常执行并返回摘要。"""
        from tasks import scheduled_tasks

        entries = [_make_entry()]
        factory = _make_task_db_session(entries)

        with (
            patch("app.database.task_db_session", new=factory),
            patch("tasks.scheduled_tasks.celery_app") as mock_celery,
        ):
            mock_celery.send_task = MagicMock()
            result = scheduled_tasks.flush_task_outbox()

        assert result["status"] == "success"
        assert result["candidates"] == 1
        assert result["dispatched"] == 1
        assert result["dead"] == 0

    @pytest.mark.asyncio
    async def test_pending_dispatched_and_marked_sent(self) -> None:
        """pending 记录补投成功 → send_task 按名字派发 + 状态置 sent。"""
        from tasks import scheduled_tasks

        entries = [_make_entry()]
        factory = _make_task_db_session(entries)

        with (
            patch("app.database.task_db_session", new=factory),
            patch("tasks.scheduled_tasks.celery_app") as mock_celery,
        ):
            mock_celery.send_task = MagicMock()
            result = await scheduled_tasks._flush_task_outbox_async()

        assert result["candidates"] == 1
        assert result["dispatched"] == 1
        assert result["failed"] == 0
        mock_celery.send_task.assert_called_once_with(
            entries[0].task_name, kwargs=entries[0].task_kwargs
        )

    @pytest.mark.asyncio
    async def test_dispatch_failure_increments_attempts(self) -> None:
        """补投失败 → attempts+1（status 更新语句被发出）。"""
        from tasks import scheduled_tasks

        entries = [_make_entry(attempts=1)]
        factory = _make_task_db_session(entries)

        with (
            patch("app.database.task_db_session", new=factory),
            patch("tasks.scheduled_tasks.celery_app") as mock_celery,
        ):
            mock_celery.send_task = MagicMock(side_effect=Exception("broker still down"))
            result = await scheduled_tasks._flush_task_outbox_async()

        assert result["dispatched"] == 0
        assert result["failed"] == 1
        assert result["dead"] == 0  # attempts=2 < 5，未达上限

    @pytest.mark.asyncio
    async def test_max_attempts_marks_dead(self) -> None:
        """attempts 达上限（第 5 次补投仍失败）→ 置 dead 防毒丸。"""
        from tasks import scheduled_tasks

        entries = [_make_entry(attempts=4)]
        factory = _make_task_db_session(entries)

        with (
            patch("app.database.task_db_session", new=factory),
            patch("tasks.scheduled_tasks.celery_app") as mock_celery,
        ):
            mock_celery.send_task = MagicMock(side_effect=Exception("broker down forever"))
            result = await scheduled_tasks._flush_task_outbox_async()

        assert result["failed"] == 1
        assert result["dead"] == 1

    @pytest.mark.asyncio
    async def test_empty_outbox_is_noop(self) -> None:
        """无 pending 记录 → 零开销空跑。"""
        from tasks import scheduled_tasks

        factory = _make_task_db_session([])

        with (
            patch("app.database.task_db_session", new=factory),
            patch("tasks.scheduled_tasks.celery_app") as mock_celery,
        ):
            mock_celery.send_task = MagicMock()
            result = await scheduled_tasks._flush_task_outbox_async()

        assert result["candidates"] == 0
        assert result["dispatched"] == 0
        mock_celery.send_task.assert_not_called()


# ==================================================================
# beat_schedule 注册
# ==================================================================

class TestBeatScheduleRegistration:
    """flush_task_outbox 已注册到 beat，每 5 分钟。"""

    def test_flush_outbox_registered(self) -> None:
        import celery_app as celery_app_module

        entry = celery_app_module.celery_app.conf.beat_schedule.get(
            "flush-task-outbox-5min"
        )
        assert entry is not None
        assert entry["task"] == "tasks.scheduled_tasks.flush_task_outbox"
        # crontab(minute="*/5") — repr 含原始分钟表达式
        assert "*/5" in str(entry["schedule"])


# ==================================================================
# 调用点接入 — 5 处一次性触发链路均已换用 Outbox 助手
# ==================================================================

class TestCallSiteIntegration:
    """源码级检查：原 try/delay/except-log 模式已被 dispatch_with_outbox 替换。"""

    def _assert_uses_outbox(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "dispatch_with_outbox" in src, f"{path} 未接入 Outbox 助手"

    def test_feedback_service_uses_outbox(self) -> None:
        self._assert_uses_outbox("app/services/feedback_service.py")

    def test_qa_service_uses_outbox(self) -> None:
        self._assert_uses_outbox("app/services/qa_service.py")

    def test_document_tasks_uses_outbox(self) -> None:
        self._assert_uses_outbox("tasks/document_tasks.py")

    def test_compounding_service_uses_outbox(self) -> None:
        self._assert_uses_outbox(
            "app/services/knowledge_compounding/compounding_service.py"
        )

    def test_video_tasks_uses_outbox_sync(self) -> None:
        with open("tasks/video_tasks.py", encoding="utf-8") as f:
            src = f.read()
        assert "dispatch_with_outbox_sync" in src
