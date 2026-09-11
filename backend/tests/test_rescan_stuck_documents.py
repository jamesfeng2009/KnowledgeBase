"""P1 补偿扫描卡死文档单测 — rescan_stuck_documents。

覆盖：
    - Celery 任务入口：禁用开关 / 正常调度
    - 异步实现：卡死重投 / 进度存活跳过 / 锁存活跳过 /
      Redis 不可用不重投 / 投递失败容忍 / tenant_id 透传
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ==================================================================
# 辅助 — 构造 mock Document / mock 环境
# ==================================================================

def _make_doc(
    doc_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    updated_at: datetime | None = None,
) -> MagicMock:
    """构造 mock Document 对象（卡死候选）。"""
    doc = MagicMock()
    doc.id = doc_id or uuid.uuid4()
    doc.tenant_id = tenant_id
    doc.updated_at = updated_at or (
        datetime.now(timezone.utc) - timedelta(hours=3)
    )
    return doc


def _make_settings(enabled: bool = True) -> MagicMock:
    """构造 mock Settings。"""
    settings = MagicMock()
    settings.DOCUMENT_RESCAN_ENABLED = enabled
    settings.DOCUMENT_RESCAN_STUCK_HOURS = 2
    settings.DOCUMENT_RESCAN_BATCH_SIZE = 50
    settings.REDIS_URL = "redis://localhost:6379/0"
    settings.TASK_LOCK_REDIS_PREFIX = "lock:task:"
    return settings


def _make_redis_client(
    progress_keys: set[str] | None = None,
    lock_keys: set[str] | None = None,
) -> MagicMock:
    """构造 mock Redis 客户端 — exists() 按预置 key 集合返回。"""
    progress_keys = progress_keys or set()
    lock_keys = lock_keys or set()
    client = MagicMock()
    client.exists = MagicMock(
        side_effect=lambda key: 1 if key in (progress_keys | lock_keys) else 0
    )
    return client


def _make_db_session(docs: list[MagicMock]) -> MagicMock:
    """构造 mock task_db_session 上下文。"""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = docs
    mock_session.execute = AsyncMock(return_value=mock_result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


# ==================================================================
# Celery 任务入口
# ==================================================================

class TestRescanCeleryTask:
    """rescan_stuck_documents Celery 任务。"""

    def test_disabled_returns_zero(self) -> None:
        """DOCUMENT_RESCAN_ENABLED=False → 直接返回，不扫描。"""
        from tasks.scheduled_tasks import rescan_stuck_documents

        mock_settings = _make_settings(enabled=False)

        with patch("app.config.get_settings", return_value=mock_settings):
            result = rescan_stuck_documents()

        assert result["candidates"] == 0
        assert result["dispatched"] == 0
        assert "禁用" in result.get("message", "")

    def test_enabled_runs_scan(self) -> None:
        """ENABLED=True → 调用 _rescan_stuck_documents_async。"""
        from tasks.scheduled_tasks import rescan_stuck_documents

        mock_settings = _make_settings(enabled=True)

        expected = {
            "status": "success",
            "candidates": 3,
            "skipped_alive": 1,
            "dispatched": 2,
            "dispatch_failed": 0,
        }
        with patch("app.config.get_settings", return_value=mock_settings), patch(
            "tasks.scheduled_tasks._rescan_stuck_documents_async",
            new=AsyncMock(return_value=expected),
        ) as mock_scan_async:
            result = rescan_stuck_documents()

        assert result == expected
        mock_scan_async.assert_awaited_once()


# ==================================================================
# 异步实现 — 卡死判定与重投
# ==================================================================

class TestRescanAsync:
    """_rescan_stuck_documents_async 核心逻辑。"""

    @pytest.mark.asyncio
    async def test_redispatch_dead_docs(self) -> None:
        """进度 key 与锁均已消失 → 判定卡死，重投 process_document。"""
        from tasks.scheduled_tasks import _rescan_stuck_documents_async

        tenant_id = uuid.uuid4()
        docs = [_make_doc(tenant_id=tenant_id), _make_doc()]
        settings = _make_settings()
        client = _make_redis_client()  # 所有 key 均不存在

        with patch("app.database.task_db_session", return_value=_make_db_session(docs)), \
             patch("redis.from_url", return_value=client), \
             patch("tasks.document_tasks.process_document.delay") as mock_delay:
            result = await _rescan_stuck_documents_async(settings)

        assert result["candidates"] == 2
        assert result["skipped_alive"] == 0
        assert result["dispatched"] == 2
        assert result["dispatch_failed"] == 0
        assert mock_delay.call_count == 2
        # tenant_id 正确透传（None 安全转换）
        first_call = mock_delay.call_args_list[0]
        assert first_call.kwargs.get("tenant_id") == str(tenant_id)
        assert mock_delay.call_args_list[1].kwargs.get("tenant_id") is None

    @pytest.mark.asyncio
    async def test_skip_when_progress_alive(self) -> None:
        """解析进度 key 存活（TTL 30min 内有写入）→ worker 仍在推进，跳过。"""
        from tasks.scheduled_tasks import _rescan_stuck_documents_async

        doc = _make_doc()
        settings = _make_settings()
        client = _make_redis_client(
            progress_keys={f"ekb:parse_progress:{doc.id}"}
        )

        with patch("app.database.task_db_session", return_value=_make_db_session([doc])), \
             patch("redis.from_url", return_value=client), \
             patch("tasks.document_tasks.process_document.delay") as mock_delay:
            result = await _rescan_stuck_documents_async(settings)

        assert result["skipped_alive"] == 1
        assert result["dispatched"] == 0
        mock_delay.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_when_lock_alive(self) -> None:
        """任务幂等锁存活 → worker 持有任务中，跳过。"""
        from tasks.scheduled_tasks import _rescan_stuck_documents_async

        doc = _make_doc()
        settings = _make_settings()
        client = _make_redis_client(
            lock_keys={f"lock:task:process_document:{doc.id}"}
        )

        with patch("app.database.task_db_session", return_value=_make_db_session([doc])), \
             patch("redis.from_url", return_value=client), \
             patch("tasks.document_tasks.process_document.delay") as mock_delay:
            result = await _rescan_stuck_documents_async(settings)

        assert result["skipped_alive"] == 1
        assert result["dispatched"] == 0
        mock_delay.assert_not_called()

    @pytest.mark.asyncio
    async def test_redis_unavailable_no_dispatch(self) -> None:
        """Redis 连接失败 → 本轮放弃重投（broker 同为 Redis，投递必败）。"""
        from tasks.scheduled_tasks import _rescan_stuck_documents_async

        docs = [_make_doc()]
        settings = _make_settings()

        with patch("app.database.task_db_session", return_value=_make_db_session(docs)), \
             patch("redis.from_url", side_effect=ConnectionError("redis down")), \
             patch("tasks.document_tasks.process_document.delay") as mock_delay:
            result = await _rescan_stuck_documents_async(settings)

        assert result["candidates"] == 1
        assert result["dispatched"] == 0
        mock_delay.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_failure_tolerated(self) -> None:
        """单个文档投递失败 → 计数 dispatch_failed，不中断整体扫描。"""
        from tasks.scheduled_tasks import _rescan_stuck_documents_async

        docs = [_make_doc(), _make_doc()]
        settings = _make_settings()
        client = _make_redis_client()

        with patch("app.database.task_db_session", return_value=_make_db_session(docs)), \
             patch("redis.from_url", return_value=client), \
             patch(
                 "tasks.document_tasks.process_document.delay",
                 side_effect=[Exception("broker down"), None],
             ) as mock_delay:
            result = await _rescan_stuck_documents_async(settings)

        assert result["candidates"] == 2
        assert result["dispatched"] == 1
        assert result["dispatch_failed"] == 1
        assert mock_delay.call_count == 2

    @pytest.mark.asyncio
    async def test_redis_check_error_skips_doc(self) -> None:
        """Redis exists() 抖动 → 跳过该文档下轮再查（宁漏勿重）。"""
        from tasks.scheduled_tasks import _rescan_stuck_documents_async

        docs = [_make_doc()]
        settings = _make_settings()
        client = MagicMock()
        client.exists = MagicMock(side_effect=ConnectionError("timeout"))

        with patch("app.database.task_db_session", return_value=_make_db_session(docs)), \
             patch("redis.from_url", return_value=client), \
             patch("tasks.document_tasks.process_document.delay") as mock_delay:
            result = await _rescan_stuck_documents_async(settings)

        assert result["dispatched"] == 0
        assert result["dispatch_failed"] == 0
        mock_delay.assert_not_called()
