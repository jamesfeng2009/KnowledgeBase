"""
知识回流层 Celery 任务 — 异步执行知识提取 / 冲突检测 / 复用注入。

将耗时的 LLM 调用从 HTTP 请求中剥离，避免请求超时：
    extract_knowledge_task     — 从执行结果异步提取知识资产
    detect_conflicts_task      — 异步检测知识冲突
    inject_for_reuse_task      — 异步复用注入

在 ``celery_app`` 不可用时（如开发环境）优雅降级，仅输出告警日志。
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


# ======================================================================
# 异步业务逻辑
# ======================================================================


async def _extract_knowledge(
    execution_id: str,
    trigger_source: str = "execution_completed",
    tenant_id: str | None = None,
) -> dict:
    """异步执行知识提取 — 调用 KnowledgeCompoundingService。"""
    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.services.knowledge_compounding import KnowledgeCompoundingService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("compounding.llm_unavailable", error=str(exc))
            llm = None

        service = KnowledgeCompoundingService(llm, db, tenant_id=tid)
        try:
            result = await service.extract_knowledge(
                execution_id, trigger_source=trigger_source
            )
            await db.commit()
            logger.info(
                "compounding.knowledge_extracted",
                execution_id=execution_id,
                status=result.get("status"),
                asset_count=result.get("asset_count", 0),
            )
            return result
        except Exception as exc:
            await db.rollback()
            logger.error(
                "compounding.knowledge_extraction_failed",
                execution_id=execution_id,
                error=str(exc),
            )
            return {
                "execution_id": execution_id,
                "status": "failed",
                "error": str(exc),
            }


async def _detect_conflicts(
    asset_id: str, tenant_id: str | None = None
) -> dict:
    """异步执行冲突检测 — 调用 KnowledgeCompoundingService。"""
    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.services.knowledge_compounding import KnowledgeCompoundingService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("compounding.llm_unavailable", error=str(exc))
            llm = None

        service = KnowledgeCompoundingService(llm, db, tenant_id=tid)
        try:
            conflicts = await service.detect_conflicts(asset_id)
            await db.commit()
            logger.info(
                "compounding.conflicts_detected",
                asset_id=asset_id,
                count=len(conflicts),
            )
            return {
                "asset_id": asset_id,
                "status": "success",
                "conflicts": conflicts,
                "count": len(conflicts),
            }
        except Exception as exc:
            await db.rollback()
            logger.error(
                "compounding.conflict_detection_failed",
                asset_id=asset_id,
                error=str(exc),
            )
            return {
                "asset_id": asset_id,
                "status": "failed",
                "error": str(exc),
            }


async def _inject_for_reuse(
    requirement_id: str,
    max_assets: int = 5,
    tenant_id: str | None = None,
) -> dict:
    """异步执行复用注入 — 调用 KnowledgeCompoundingService。"""
    from app.database import task_db_session
    from app.llm.factory import get_llm_provider
    from app.services.knowledge_compounding import KnowledgeCompoundingService

    tid = uuid.UUID(tenant_id) if tenant_id else None

    async with task_db_session() as db:
        try:
            llm = get_llm_provider()
        except Exception as exc:
            logger.warning("compounding.llm_unavailable", error=str(exc))
            llm = None

        service = KnowledgeCompoundingService(llm, db, tenant_id=tid)
        try:
            result = await service.inject_for_reuse(
                requirement_id, max_assets=max_assets
            )
            await db.commit()
            logger.info(
                "compounding.reuse_injected",
                requirement_id=requirement_id,
                status=result.get("status"),
                asset_count=result.get("asset_count", 0),
            )
            return result
        except Exception as exc:
            await db.rollback()
            logger.error(
                "compounding.reuse_injection_failed",
                requirement_id=requirement_id,
                error=str(exc),
            )
            return {
                "requirement_id": requirement_id,
                "status": "failed",
                "error": str(exc),
            }


# ======================================================================
# Celery 任务定义 — 延迟导入 celery_app 避免循环依赖
# ======================================================================

try:
    from celery_app import celery_app

    @celery_app.task(name="tasks.compounding_tasks.extract_knowledge_task")
    def extract_knowledge_task(
        execution_id: str,
        trigger_source: str = "execution_completed",
        tenant_id: str | None = None,
    ) -> dict:
        """异步从测试执行结果提取知识资产。

        串联知识回流 5 步中的 Step 1~4：
            收集执行结果 → AI 知识提取 → 知识资产沉淀 → 冲突检测

        Args:
            execution_id: 执行记录 ID（UUID 字符串）。
            trigger_source: 触发来源（execution_completed/manual/scheduled）。
            tenant_id: 租户 ID（UUID 字符串），用于多租户数据隔离。

        Returns:
            处理结果摘要，含 status / asset_count / conflicts 等字段。
        """
        logger.info(
            "compounding.extract_knowledge_task_started",
            execution_id=execution_id,
        )
        try:
            result = _run_async(
                _extract_knowledge(execution_id, trigger_source, tenant_id)
            )
            logger.info(
                "compounding.extract_knowledge_task_completed",
                execution_id=execution_id,
                status=result.get("status"),
            )
            return result
        except Exception as exc:
            logger.error(
                "compounding.extract_knowledge_task_failed",
                execution_id=execution_id,
                error=str(exc),
            )
            return {
                "execution_id": execution_id,
                "status": "failed",
                "error": str(exc),
            }

    @celery_app.task(name="tasks.compounding_tasks.detect_conflicts_task")
    def detect_conflicts_task(
        asset_id: str, tenant_id: str | None = None
    ) -> dict:
        """异步检测知识资产冲突。

        Args:
            asset_id: 知识资产 ID（UUID 字符串）。
            tenant_id: 租户 ID（UUID 字符串），用于多租户数据隔离。

        Returns:
            检测到的冲突列表。
        """
        logger.info(
            "compounding.detect_conflicts_task_started",
            asset_id=asset_id,
        )
        try:
            result = _run_async(_detect_conflicts(asset_id, tenant_id))
            logger.info(
                "compounding.detect_conflicts_task_completed",
                asset_id=asset_id,
                count=result.get("count", 0),
            )
            return result
        except Exception as exc:
            logger.error(
                "compounding.detect_conflicts_task_failed",
                asset_id=asset_id,
                error=str(exc),
            )
            return {
                "asset_id": asset_id,
                "status": "failed",
                "error": str(exc),
            }

    @celery_app.task(name="tasks.compounding_tasks.inject_for_reuse_task")
    def inject_for_reuse_task(
        requirement_id: str,
        max_assets: int = 5,
        tenant_id: str | None = None,
    ) -> dict:
        """异步复用注入 — 检索历史知识资产注入用例生成上下文。

        Args:
            requirement_id: 需求点 ID（UUID 字符串）。
            max_assets: 最大注入资产数，默认 5。
            tenant_id: 租户 ID（UUID 字符串），用于多租户数据隔离。

        Returns:
            注入结果摘要，含 injected_assets / injection_context / asset_count。
        """
        logger.info(
            "compounding.inject_for_reuse_task_started",
            requirement_id=requirement_id,
        )
        try:
            result = _run_async(
                _inject_for_reuse(requirement_id, max_assets, tenant_id)
            )
            logger.info(
                "compounding.inject_for_reuse_task_completed",
                requirement_id=requirement_id,
                status=result.get("status"),
            )
            return result
        except Exception as exc:
            logger.error(
                "compounding.inject_for_reuse_task_failed",
                requirement_id=requirement_id,
                error=str(exc),
            )
            return {
                "requirement_id": requirement_id,
                "status": "failed",
                "error": str(exc),
            }

except ImportError:
    logger.warning("compounding.celery_not_available")
