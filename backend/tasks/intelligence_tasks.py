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
    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.services.doc_intelligence_service import DocIntelligenceService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("intelligence.llm_unavailable", error=str(exc))
            return {"doc_id": doc_id, "status": "skipped", "reason": "llm_unavailable"}

        service = DocIntelligenceService(llm, db, tenant_id=tid)
        result = await service.process_all(doc_id)
        await db.commit()
        return result


async def _backfill_constraints(
    kb_id: str, tenant_id: str | None = None, batch_size: int = 50
) -> dict:
    """异步执行存量文档约束补标 — 逐批处理 KB 内已发布文档。

    只跑 extract_constraints（不重跑摘要/标签，省 LLM 成本）；
    幂等：重跑时旧规则走版本链 retire（superseded_by 回填）。
    """
    from sqlalchemy import select

    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.models.knowledge import Document
    from app.services.doc_intelligence_service import DocIntelligenceService

    tid = uuid.UUID(tenant_id) if tenant_id else None
    kb_uuid = uuid.UUID(kb_id)
    stats = {"kb_id": kb_id, "processed": 0, "docs_with_rules": 0, "failed": 0}

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("backfill.llm_unavailable", error=str(exc))
            return {**stats, "status": "skipped", "reason": "llm_unavailable"}

        service = DocIntelligenceService(llm, db, tenant_id=tid)
        stmt = (
            select(Document.id)
            .where(Document.kb_id == kb_uuid, Document.status == "published")
            .order_by(Document.created_at)
        )
        doc_ids = list((await db.execute(stmt)).scalars())

        for start in range(0, len(doc_ids), batch_size):
            batch = doc_ids[start : start + batch_size]
            for doc_id in batch:
                doc = await db.get(Document, doc_id)
                if doc is None:
                    continue
                try:
                    saved = await service.extract_constraints(doc)
                    await db.commit()
                    stats["processed"] += 1
                    if saved:
                        stats["docs_with_rules"] += 1
                except Exception as exc:
                    await db.rollback()
                    stats["failed"] += 1
                    logger.warning(
                        "backfill.doc_failed",
                        doc_id=str(doc_id),
                        error=str(exc)[:200],
                    )
            logger.info(
                "backfill.progress",
                kb_id=kb_id,
                processed=stats["processed"],
                total=len(doc_ids),
            )
    stats["status"] = "completed"
    return stats


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

    @celery_app.task(
        name="tasks.intelligence_tasks.backfill_constraints",
        **make_celery_retry_kwargs(),
    )
    def backfill_constraints(
        kb_id: str, tenant_id: str | None = None, batch_size: int = 50
    ) -> dict:
        """存量文档约束补标（P2 · GAP-3）— 按 KB 批量重跑约束抽取。

        只跑 extract_constraints（不重跑摘要/标签，省 LLM 成本）；
        Stage A 正则预筛保证无约束语言的文档零 LLM 调用。
        幂等：重跑时旧规则走版本链 retire（superseded_by 回填，禁 DELETE）。

        Args:
            kb_id: 知识库 ID（UUID 字符串）。
            tenant_id: 租户 ID（UUID 字符串），多租户隔离。
            batch_size: 每批文档数（批间 commit，防长事务）。

        Returns:
            统计摘要 {processed/docs_with_rules/failed/status}。
        """
        logger.info("backfill.task_started", kb_id=kb_id)
        try:
            result = _run_async(_backfill_constraints(kb_id, tenant_id, batch_size))
            logger.info("backfill.task_completed", kb_id=kb_id, result=result)
            return result
        except Exception as exc:
            logger.error("backfill.task_failed", kb_id=kb_id, error=str(exc))
            return {"kb_id": kb_id, "status": "failed", "error": str(exc)}

except ImportError:
    logger.warning("intelligence.celery_not_available")
