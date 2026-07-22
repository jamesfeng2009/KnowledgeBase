"""
文档智能处理 Celery 任务 — 文档入库后自动执行摘要/标签/分类/行动项。

在 document_tasks.process_document 完成后链式调用：
    process_document.delay(doc_id) → process_intelligence.delay(doc_id)
"""

from __future__ import annotations

import asyncio
import uuid

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _run_async(coro):
    """在同步 Celery 任务中执行异步协程。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _process_intelligence(
    doc_id: str, tenant_id: str | None = None
) -> dict:
    """异步执行文档智能处理。"""
    from app.database import async_session_factory
    from app.llm.factory import get_llm_provider
    from app.services.doc_intelligence_service import DocIntelligenceService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with async_session_factory() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("intelligence.llm_unavailable", error=str(exc))
            return {"doc_id": doc_id, "status": "skipped", "reason": "llm_unavailable"}

        service = DocIntelligenceService(llm, db, tenant_id=tid)
        result = await service.process_all(doc_id)
        await db.commit()
        return result


# 延迟导入 celery_app，避免循环依赖
try:
    from celery_app import celery_app
    from app.utils.retry import make_celery_retry_kwargs

    @celery_app.task(
        name="tasks.intelligence_tasks.process_intelligence",
        **make_celery_retry_kwargs(),
    )
    def process_intelligence(
        doc_id: str, tenant_id: str | None = None
    ) -> dict:
        """文档入库后自动执行智能处理。

        执行五项自动化：
        1. 自动摘要（200 字）
        2. 自动标签（3-5 个关键词）
        3. 自动分类（7 种文档类别）
        4. 行动项提取（仅会议纪要）
        5. FAQ 自动生成（可选）

        LLM 不可用时优雅降级，不阻塞文档入库流程。

        Args:
            doc_id: 文档 ID（UUID 字符串）。
            tenant_id: 租户 ID（UUID 字符串），用于多租户数据隔离。

        Returns:
            处理结果摘要。
        """
        logger.info("intelligence.task_started", doc_id=doc_id)
        try:
            result = _run_async(_process_intelligence(doc_id, tenant_id))
            logger.info("intelligence.task_completed", doc_id=doc_id, result=result)
            return result
        except Exception as exc:
            logger.error("intelligence.task_failed", doc_id=doc_id, error=str(exc))
            return {"doc_id": doc_id, "status": "failed", "error": str(exc)}

except ImportError:
    logger.warning("intelligence.celery_not_available")
