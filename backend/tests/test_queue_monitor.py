"""任务队列监控单测 — P1 任务队列可视化面板。

测试策略：
    - 队列清单：从 celery_app._ALL_QUEUES 读取（含死信队列）；
    - RabbitMQ API：mock httpx 验证深度解析、非 200 与网络异常降级；
    - outbox 统计：mock db.execute 验证分组计数与死信占比；
    - overview：并发汇总两个数据源。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.queue_monitor_service import (
    QueueMonitorService,
    get_all_queue_names,
)


class TestQueueNames:
    def test_includes_dead_letter(self) -> None:
        names = get_all_queue_names()
        assert "celery" in names
        assert "documents" in names
        assert "dead_letter" in names


def _make_service(db=None) -> QueueMonitorService:
    with patch("app.services.queue_monitor_service.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.RABBITMQ_MGMT_URL = "http://rabbitmq:15672"
        s.RABBITMQ_MGMT_USER = "guest"
        s.RABBITMQ_MGMT_PASSWORD = "guest"
        return QueueMonitorService(db or MagicMock())


class TestQueueDepths:
    @pytest.mark.asyncio
    async def test_parses_queue_depths(self) -> None:
        service = _make_service()
        mgmt_data = [
            {"name": "celery", "messages": 10, "messages_ready": 8,
             "messages_unacknowledged": 2, "consumers": 2},
            {"name": "dead_letter", "messages": 3, "messages_ready": 3,
             "messages_unacknowledged": 0, "consumers": 0},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mgmt_data
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch(
            "app.services.queue_monitor_service.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await service.list_queue_depths()

        by_name = {q["queue"]: q for q in result}
        assert by_name["celery"]["ready"] == 8
        assert by_name["celery"]["unacked"] == 2
        assert by_name["celery"]["total"] == 10
        assert by_name["dead_letter"]["ready"] == 3
        assert by_name["documents"]["state"] == "missing"
        # 鉴权使用 basic auth
        assert mock_client.get.call_args.kwargs["auth"] == ("guest", "guest")

    @pytest.mark.asyncio
    async def test_mgmt_error_degrades(self) -> None:
        service = _make_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch(
            "app.services.queue_monitor_service.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await service.list_queue_depths()

        assert all(q["state"] == "unavailable" for q in result)
        assert all(q["ready"] is None for q in result)

    @pytest.mark.asyncio
    async def test_network_error_degrades(self) -> None:
        service = _make_service()
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("conn refused")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch(
            "app.services.queue_monitor_service.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await service.list_queue_depths()

        assert all(q["state"] == "unavailable" for q in result)


class TestOutboxStats:
    @pytest.mark.asyncio
    async def test_counts_by_status(self) -> None:
        db = MagicMock()
        service = _make_service(db)
        rows = [("pending", 5), ("sent", 100), ("dead", 2)]
        result_proxy = SimpleNamespace(all=lambda: rows)
        db.execute = AsyncMock(return_value=result_proxy)

        stats = await service.outbox_stats()

        assert stats["pending"] == 5
        assert stats["sent"] == 100
        assert stats["dead"] == 2
        assert stats["total"] == 107
        assert stats["dead_ratio"] == round(2 / 107, 4)

    @pytest.mark.asyncio
    async def test_empty_db(self) -> None:
        db = MagicMock()
        service = _make_service(db)
        db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: []))

        stats = await service.outbox_stats()
        assert stats["total"] == 0
        assert stats["dead_ratio"] == 0.0

    @pytest.mark.asyncio
    async def test_db_error_degrades(self) -> None:
        db = MagicMock()
        service = _make_service(db)
        db.execute = AsyncMock(side_effect=Exception("db down"))

        stats = await service.outbox_stats()
        assert stats["total"] == 0


class TestOverview:
    @pytest.mark.asyncio
    async def test_overview_combines_sources(self) -> None:
        db = MagicMock()
        service = _make_service(db)
        service.list_queue_depths = AsyncMock(return_value=[{"queue": "celery"}])
        service.outbox_stats = AsyncMock(return_value={"total": 1})

        data = await service.overview()
        assert data["queues"] == [{"queue": "celery"}]
        assert data["outbox"]["total"] == 1


class TestQueueMonitorAPI:
    def test_router_registered(self) -> None:
        from app.api.v1.queue_monitor import router

        paths = {r.path for r in router.routes}
        assert "/observability/queues" in paths
