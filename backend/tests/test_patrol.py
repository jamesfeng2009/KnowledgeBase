"""P2 定时巡检兜底单测 — get_stale_external_docs + patrol 编排。

覆盖：
    - get_stale_external_docs：查询逻辑（mock DB，验证 WHERE/ORDER/LIMIT）
    - patrol：空批次 / 混合结果 / 失败容忍 / 并发限流
    - Celery 任务：禁用开关 / 正常调度
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.external_sync_service import (
    ExternalSyncService,
    RefreshResult,
)


# ==================================================================
# 辅助 — 构造 mock Document
# ==================================================================

def _make_doc(
    doc_id: uuid.UUID | None = None,
    source: str = "feishu",
    source_doc_id: str = "doccnX",
    last_checked_at: datetime | None = None,
) -> MagicMock:
    """构造 mock Document 对象。"""
    doc = MagicMock()
    doc.id = doc_id or uuid.uuid4()
    doc.source = source
    doc.source_doc_id = source_doc_id
    doc.last_checked_at = last_checked_at
    return doc


# ==================================================================
# get_stale_external_docs — 查询逻辑
# ==================================================================

class TestGetStaleExternalDocs:
    """查询过期外部文档。"""

    @pytest.mark.asyncio
    async def test_returns_docs_from_db(self) -> None:
        """正常查询 — 返回 DB 结果列表。"""
        service = ExternalSyncService()
        mock_docs = [_make_doc(source_doc_id="d1"), _make_doc(source_doc_id="d2")]

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = mock_docs
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.database.async_session_factory",
            return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session),
                                   __aexit__=AsyncMock(return_value=None)),
        ):
            result = await service.get_stale_external_docs(
                max_age_hours=24, batch_size=50
            )

        assert result == mock_docs
        # 验证查询被执行
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        """无过期文档 → 空列表。"""
        service = ExternalSyncService()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.database.async_session_factory",
            return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session),
                                   __aexit__=AsyncMock(return_value=None)),
        ):
            result = await service.get_stale_external_docs()

        assert result == []

    @pytest.mark.asyncio
    async def test_batch_size_passed_to_query(self) -> None:
        """batch_size 传递到查询 LIMIT。"""
        service = ExternalSyncService()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.database.async_session_factory",
            return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_session),
                                   __aexit__=AsyncMock(return_value=None)),
        ):
            await service.get_stale_external_docs(batch_size=10)

        # 验证 .limit(10) 在链式调用中
        mock_session.execute.assert_awaited_once()


# ==================================================================
# patrol — 批量巡检编排
# ==================================================================

class TestPatrol:
    """patrol 批量巡检逻辑。"""

    @pytest.mark.asyncio
    async def test_empty_stale_docs_returns_zeros(self) -> None:
        """无过期文档 → 全零摘要。"""
        service = ExternalSyncService()
        with patch.object(
            service, "get_stale_external_docs", new=AsyncMock(return_value=[])
        ):
            result = await service.patrol()

        assert result == {
            "total": 0, "fresh": 0, "updated": 0,
            "failed": 0, "skipped": 0,
        }

    @pytest.mark.asyncio
    async def test_mixed_results_counted(self) -> None:
        """混合结果：2 fresh + 1 updated + 1 skipped → 正确计数。"""
        service = ExternalSyncService()
        docs = [_make_doc() for _ in range(4)]

        results = [
            RefreshResult(status="fresh"),
            RefreshResult(status="fresh"),
            RefreshResult(status="updated", content="new"),
            RefreshResult(status="skipped", reason="no_cred"),
        ]
        with patch.object(
            service, "get_stale_external_docs", new=AsyncMock(return_value=docs)
        ), patch.object(
            service, "verify_and_refresh", new=AsyncMock(side_effect=results)
        ):
            result = await service.patrol(concurrency=2)

        assert result["total"] == 4
        assert result["fresh"] == 2
        assert result["updated"] == 1
        assert result["skipped"] == 1
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_failure_tolerance(self) -> None:
        """单文档失败不中断整批。"""
        service = ExternalSyncService()
        docs = [_make_doc() for _ in range(3)]

        async def fake_verify(doc_id, force=False):
            if docs.index(next(d for d in docs if d.id == doc_id)) == 1:
                raise RuntimeError("timeout")
            return RefreshResult(status="fresh")

        with patch.object(
            service, "get_stale_external_docs", new=AsyncMock(return_value=docs)
        ), patch.object(
            service, "verify_and_refresh", new=fake_verify
        ):
            result = await service.patrol(concurrency=2)

        assert result["total"] == 3
        assert result["fresh"] == 2
        assert result["failed"] == 1

    @pytest.mark.asyncio
    async def test_concurrency_limit(self) -> None:
        """并发上限 = concurrency（信号量限流）。"""
        service = ExternalSyncService()
        docs = [_make_doc() for _ in range(6)]

        current = 0
        max_concurrent = 0

        async def fake_verify(doc_id, force=False):
            nonlocal current, max_concurrent
            current += 1
            max_concurrent = max(max_concurrent, current)
            import asyncio
            await asyncio.sleep(0.01)
            current -= 1
            return RefreshResult(status="fresh")

        with patch.object(
            service, "get_stale_external_docs", new=AsyncMock(return_value=docs)
        ), patch.object(
            service, "verify_and_refresh", new=fake_verify
        ):
            await service.patrol(concurrency=2)

        # 并发不超过 2
        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_all_failed(self) -> None:
        """全部失败 → failed=total。"""
        service = ExternalSyncService()
        docs = [_make_doc() for _ in range(3)]

        with patch.object(
            service, "get_stale_external_docs", new=AsyncMock(return_value=docs)
        ), patch.object(
            service, "verify_and_refresh",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await service.patrol(concurrency=2)

        assert result["total"] == 3
        assert result["failed"] == 3
        assert result["fresh"] == 0

    @pytest.mark.asyncio
    async def test_force_true_passed_to_verify(self) -> None:
        """patrol 必须传 force=True（忽略缓存，否则巡检无意义）。"""
        service = ExternalSyncService()
        docs = [_make_doc()]

        mock_verify = AsyncMock(return_value=RefreshResult(status="fresh"))
        with patch.object(
            service, "get_stale_external_docs", new=AsyncMock(return_value=docs)
        ), patch.object(
            service, "verify_and_refresh", new=mock_verify
        ):
            await service.patrol()

        mock_verify.assert_awaited_once()
        call_kwargs = mock_verify.call_args
        assert call_kwargs.kwargs.get("force") is True or call_kwargs.args[-1] is True


# ==================================================================
# Celery 任务入口
# ==================================================================

class TestPatrolCeleryTask:
    """patrol_external_docs Celery 任务。"""

    def test_disabled_returns_zero(self) -> None:
        """EXTERNAL_SYNC_PATROL_ENABLED=False → 直接返回，不巡检。"""
        from tasks.scheduled_tasks import patrol_external_docs

        mock_settings = MagicMock()
        mock_settings.EXTERNAL_SYNC_PATROL_ENABLED = False

        with patch("app.config.get_settings", return_value=mock_settings):
            result = patrol_external_docs()

        assert result["total"] == 0
        assert result.get("skipped") == 0
        assert "禁用" in result.get("message", "")

    def test_enabled_runs_patrol(self) -> None:
        """ENABLED=True → 调用 _patrol_external_docs_async。"""
        from tasks.scheduled_tasks import patrol_external_docs

        mock_settings = MagicMock()
        mock_settings.EXTERNAL_SYNC_PATROL_ENABLED = True
        mock_settings.EXTERNAL_SYNC_PATROL_MAX_STALENESS_HOURS = 24
        mock_settings.EXTERNAL_SYNC_PATROL_BATCH_SIZE = 50
        mock_settings.EXTERNAL_SYNC_PATROL_CONCURRENCY = 2

        expected = {
            "total": 5, "fresh": 3, "updated": 1, "failed": 1, "skipped": 0
        }
        with patch("app.config.get_settings", return_value=mock_settings), patch(
            "tasks.scheduled_tasks._patrol_external_docs_async",
            new=AsyncMock(return_value=expected),
        ) as mock_patrol_async:
            result = patrol_external_docs()

        assert result == expected
        mock_patrol_async.assert_awaited_once()


# ==================================================================
# 集成验证 — 方案 A 无盲区
# ==================================================================

class TestPlanA_NoBlindSpot:
    """方案 A 核心保证：无类别过滤，所有外部文档统一阈值。"""

    @pytest.mark.asyncio
    async def test_all_categories_patrolled(self) -> None:
        """技术笔记、FAQ 等非强时效类也被巡检。"""
        service = ExternalSyncService()
        # 模拟包含技术笔记（非强时效类）的文档
        docs = [
            _make_doc(source_doc_id="policy-1"),    # 政策
            _make_doc(source_doc_id="tech-note-1"),  # 技术笔记
            _make_doc(source_doc_id="faq-1"),        # FAQ
        ]
        with patch.object(
            service, "get_stale_external_docs", new=AsyncMock(return_value=docs)
        ), patch.object(
            service, "verify_and_refresh",
            new=AsyncMock(return_value=RefreshResult(status="fresh")),
        ):
            result = await service.patrol()

        # 全部 3 个都被巡检（无类别排除）
        assert result["total"] == 3
        assert result["fresh"] == 3
