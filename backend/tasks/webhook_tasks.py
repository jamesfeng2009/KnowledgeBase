"""Webhook 同步任务 — P1 Webhook 主动同步的异步执行体。

Webhook 端点收到事件后立即返回 200，实际同步由本模块的 Celery 任务
异步执行，避免外部平台 5s 超时重试。

任务流程::

    sync_external_document(adapter_id, source_doc_id, tenant_id)
        ↓ asyncio.run(_sync_async(...))
    ExternalSyncService.force_refresh(force=True)
        ↓ P0 两阶段校验（已在 P0 实现）
        ↓ 内容变化 → 重建向量索引
"""
from __future__ import annotations

import asyncio
from typing import Any

from celery_app import celery_app

from app.utils.logger import get_logger
from app.utils.retry import make_celery_retry_kwargs

logger = get_logger(__name__)


@celery_app.task(
    name="tasks.webhook_tasks.sync_external_document",
    bind=True,
    **make_celery_retry_kwargs(),
)
def sync_external_document(
    self,
    adapter_id: str,
    source_doc_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """异步同步外部文档 — 调用 ExternalSyncService.force_refresh。

    由 webhook 端点在收到飞书/Confluence 文档更新事件后派发。
    实际校验逻辑复用 P0 的两阶段验证（force=True 强制忽略缓存）。

    Args:
        adapter_id: 适配器 ID（feishu / confluence）。
        source_doc_id: 外部文档 ID（飞书 doc_token / Confluence pageId）。
        tenant_id: 租户 ID（可选，多租户场景）。

    Returns:
        同步结果摘要 dict。
    """
    logger.info(
        "webhook.sync_task_start",
        adapter_id=adapter_id,
        source_doc_id=source_doc_id,
        tenant_id=tenant_id,
    )

    try:
        result = asyncio.run(_sync_async(adapter_id, source_doc_id, tenant_id))
        logger.info(
            "webhook.sync_task_done",
            adapter_id=adapter_id,
            source_doc_id=source_doc_id,
            status=result.status,
            sync_status=result.sync_status,
        )
        return {
            "adapter_id": adapter_id,
            "source_doc_id": source_doc_id,
            "status": result.status,
            "sync_status": result.sync_status,
            "reason": result.reason,
        }
    except Exception as exc:
        logger.error(
            "webhook.sync_task_failed",
            adapter_id=adapter_id,
            source_doc_id=source_doc_id,
            error=str(exc)[:200],
        )
        # Celery 重试机制（make_celery_retry_kwargs 已配置）
        raise self.retry(exc=exc)


async def _sync_async(
    adapter_id: str,
    source_doc_id: str,
    tenant_id: str | None,
) -> Any:
    """异步执行体 — 在 asyncio.run 事件循环中调用 force_refresh。"""
    import uuid

    from app.services.external_sync_service import get_external_sync_service

    service = get_external_sync_service()
    tenant_uuid = uuid.UUID(tenant_id) if tenant_id else None

    return await service.force_refresh(
        adapter_id=adapter_id,
        source_doc_id=source_doc_id,
        tenant_id=tenant_uuid,
    )
