"""
任务队列监控服务 — 单一职责：Celery/RabbitMQ 队列深度、死信与失败重试统计。

数据来源：
    - RabbitMQ Management API（/api/queues）— 各业务队列 ready/unacked/total；
    - task_outbox 表 — 欠投递任务（pending/sent/dead）计数，暴露失败重试健康度；
    - Celery 业务队列清单来自 ``celery_app._ALL_QUEUES``（唯一事实来源）。

降级策略：
    - Management API 不可达时队列深度返回 None（标记 unavailable），不抛异常；
    - outbox 统计依赖 DB，异常时返回空统计。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.task_outbox import TaskOutbox
from app.utils.logger import get_logger

log = get_logger(__name__)


def get_all_queue_names() -> list[str]:
    """返回 Celery 业务队列清单（含默认队列与死信队列）。"""
    try:
        from celery_app import _ALL_QUEUES

        return list(_ALL_QUEUES)
    except Exception:
        return [
            "celery", "documents", "indexing", "scheduled",
            "notifications", "multimodal", "dead_letter",
        ]


class QueueMonitorService:
    """任务队列监控服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        settings = get_settings()
        self._mgmt_url = settings.RABBITMQ_MGMT_URL.rstrip("/")
        self._mgmt_user = settings.RABBITMQ_MGMT_USER
        self._mgmt_password = settings.RABBITMQ_MGMT_PASSWORD

    # ------------------------------------------------------------------
    # RabbitMQ 队列深度
    # ------------------------------------------------------------------

    async def list_queue_depths(self) -> list[dict[str, Any]]:
        """查询所有业务队列的实时深度。

        Returns:
            [{"queue", "ready", "unacked", "total", "consumers", "state"}]
            管理 API 不可达时 ready/unacked/total 为 None。
        """
        queues = await self._fetch_mgmt_queues()
        if queues is None:
            return [
                {"queue": q, "ready": None, "unacked": None,
                 "total": None, "consumers": 0, "state": "unavailable"}
                for q in get_all_queue_names()
            ]

        by_name = {q.get("name", ""): q for q in queues}
        result: list[dict[str, Any]] = []
        for q in get_all_queue_names():
            data = by_name.get(q, {})
            result.append(
                {
                    "queue": q,
                    "ready": data.get("messages_ready"),
                    "unacked": data.get("messages_unacknowledged"),
                    "total": data.get("messages"),
                    "consumers": data.get("consumers", 0),
                    "state": "ok" if data else "missing",
                }
            )
        return result

    async def _fetch_mgmt_queues(self) -> list[dict[str, Any]] | None:
        """调用 RabbitMQ Management API 拉取全部队列（异常返回 None）。"""
        url = f"{self._mgmt_url}/api/queues"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    url, auth=(self._mgmt_user, self._mgmt_password)
                )
                if resp.status_code != 200:
                    log.warning(
                        "queue_monitor.mgmt_http_error", status=resp.status_code
                    )
                    return None
                return resp.json()
        except Exception as exc:
            log.warning("queue_monitor.mgmt_unavailable", error=str(exc)[:200])
            return None

    # ------------------------------------------------------------------
    # Outbox 统计（失败重试健康度）
    # ------------------------------------------------------------------

    async def outbox_stats(self) -> dict[str, Any]:
        """统计 task_outbox 状态分布与死信占比。"""
        try:
            rows = await self.db.execute(
                select(TaskOutbox.status, func.count())
                .group_by(TaskOutbox.status)
            )
            counts: dict[str, int] = {}
            for status, count in rows.all():
                counts[status] = int(count)
            total = sum(counts.values())
            dead = counts.get("dead", 0)
            return {
                "pending": counts.get("pending", 0),
                "sent": counts.get("sent", 0),
                "dead": dead,
                "total": total,
                "dead_ratio": round(dead / total, 4) if total else 0.0,
            }
        except Exception as exc:
            log.warning("queue_monitor.outbox_failed", error=str(exc)[:200])
            return {"pending": 0, "sent": 0, "dead": 0, "total": 0, "dead_ratio": 0.0}

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

    async def overview(self) -> dict[str, Any]:
        """队列面板总览：队列深度 + outbox 统计。"""
        depths, outbox = await asyncio.gather(
            self.list_queue_depths(), self.outbox_stats()
        )
        return {
            "queues": depths,
            "outbox": outbox,
        }
