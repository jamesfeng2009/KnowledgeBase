"""
定时任务 — 单一职责：周期性运维任务（知识缺口检测、过期预警、清理、报告）。

遵循单一职责：本模块只负责定时任务的编排，
具体业务逻辑委托对应的 Service。
遵循开闭原则：新增定时任务只需添加新的 @celery_app.task 函数，
并在 celery_app.py 的 beat_schedule 中注册调度。

定时任务调度（在 celery_app.py 的 beat_schedule 中配置）：
- detect_knowledge_gaps：每日检测高频无结果查询
- check_expiration：每日检查知识过期预警
- cleanup_expired_facts：每日清理过期记忆事实
- generate_quality_report：每周生成质量报告
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from celery_app import celery_app
from app.utils.logger import get_logger

logger = get_logger(__name__)


@celery_app.task(name="tasks.scheduled_tasks.detect_knowledge_gaps")
def detect_knowledge_gaps() -> dict[str, Any]:
    """每日检测高频无结果查询（知识缺口）。

    流程：
    1. 查询所有 open 状态的知识缺口；
    2. 筛选出高频缺口（search_count >= 阈值）；
    3. 记录日志，供管理员关注并补充知识库内容。

    Returns:
        检测结果字典，包含缺口数量。
    """
    logger.info("scheduled.detect_gaps_started")
    try:
        result = asyncio.run(_detect_gaps_async())
        logger.info(
            "scheduled.detect_gaps_completed",
            total_gaps=result.get("total_gaps", 0),
            high_freq_gaps=result.get("high_frequency_gaps", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.detect_gaps_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


@celery_app.task(name="tasks.scheduled_tasks.check_expiration")
def check_expiration() -> dict[str, Any]:
    """每日检查知识过期预警（调用 Graphiti 时间线）。

    流程：
    1. 查询即将过期（valid_to 在未来 7 天内）的知识实体；
    2. 记录预警日志，供管理员处理过期知识。

    Returns:
        预警结果字典，包含即将过期的实体数量。
    """
    logger.info("scheduled.check_expiration_started")
    try:
        result = asyncio.run(_check_expiration_async())
        logger.info(
            "scheduled.check_expiration_completed",
            expiring_count=result.get("expiring_count", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.check_expiration_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


@celery_app.task(name="tasks.scheduled_tasks.cleanup_expired_facts")
def cleanup_expired_facts() -> dict[str, Any]:
    """每日清理过期的记忆事实。

    流程：
    1. 查询所有已过期（expires_at < 当前时间）的记忆事实；
    2. 将其标记为 inactive（is_active = False）；
    3. 记录清理日志。

    Returns:
        清理结果字典，包含清理的事实数量。
    """
    logger.info("scheduled.cleanup_facts_started")
    try:
        result = asyncio.run(_cleanup_expired_facts_async())
        logger.info(
            "scheduled.cleanup_facts_completed",
            cleaned_count=result.get("cleaned_count", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.cleanup_facts_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


@celery_app.task(name="tasks.scheduled_tasks.generate_quality_report")
def generate_quality_report() -> dict[str, Any]:
    """每周生成知识质量报告。

    流程：
    1. 调用 QualityService 生成全局质量报告；
    2. 记录报告摘要日志，供管理员审阅。

    Returns:
        质量报告字典。
    """
    logger.info("scheduled.quality_report_started")
    try:
        result = asyncio.run(_generate_quality_report_async())
        logger.info(
            "scheduled.quality_report_completed",
            total_docs=result.get("total_docs", 0),
            avg_score=result.get("average_score", 0),
            low_quality=result.get("low_quality_count", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.quality_report_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ------------------------------------------------------------------
# 异步实现
# ------------------------------------------------------------------

async def _detect_gaps_async() -> dict[str, Any]:
    """异步检测知识缺口。"""
    from app.database import async_session_factory
    from app.services.gap_detector_service import GapDetectorService

    async with async_session_factory() as session:
        service = GapDetectorService(session)
        gaps = await service.detect_gaps()

        # 获取全部 open 缺口用于统计
        all_gaps = await service.get_gaps()

        return {
            "status": "success",
            "total_gaps": len(all_gaps),
            "high_frequency_gaps": len(gaps),
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }


async def _check_expiration_async() -> dict[str, Any]:
    """异步检查知识过期预警 — 调用 Graphiti 时间线。"""
    from app.database import async_session_factory
    from app.models.memory import KnowledgeEntity
    from sqlalchemy import select
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    threshold = now + timedelta(days=7)

    async with async_session_factory() as session:
        # 查询即将过期（valid_to 在未来 7 天内且非 NULL）的知识实体
        stmt = (
            select(KnowledgeEntity)
            .where(
                KnowledgeEntity.valid_to.isnot(None),
                KnowledgeEntity.valid_to <= threshold,
                KnowledgeEntity.valid_to >= now,
            )
        )
        result = await session.execute(stmt)
        expiring_entities = list(result.scalars().all())

        expiring_list = [
            {
                "id": str(e.id),
                "name": e.name,
                "entity_type": e.entity_type,
                "valid_from": e.valid_from.isoformat() if e.valid_from else None,
                "valid_to": e.valid_to.isoformat() if e.valid_to else None,
            }
            for e in expiring_entities
        ]

        await session.commit()

        return {
            "status": "success",
            "expiring_count": len(expiring_entities),
            "expiring_entities": expiring_list,
            "checked_at": now.isoformat(),
        }


async def _cleanup_expired_facts_async() -> dict[str, Any]:
    """异步清理过期的记忆事实。"""
    from app.database import async_session_factory
    from app.models.memory import MemoryFact
    from sqlalchemy import select, update
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        # 查询已过期但仍然 active 的事实
        stmt = select(MemoryFact).where(
            MemoryFact.expires_at.isnot(None),
            MemoryFact.expires_at < now,
            MemoryFact.is_active.is_(True),
        )
        result = await session.execute(stmt)
        expired_facts = list(result.scalars().all())

        # 批量标记为 inactive
        cleaned_count = 0
        for fact in expired_facts:
            fact.is_active = False
            cleaned_count += 1

        await session.commit()

        return {
            "status": "success",
            "cleaned_count": cleaned_count,
            "cleaned_at": now.isoformat(),
        }


async def _generate_quality_report_async() -> dict[str, Any]:
    """异步生成知识质量报告。"""
    from app.database import async_session_factory
    from app.services.quality_service import QualityService

    async with async_session_factory() as session:
        service = QualityService(session)
        report = await service.get_quality_report(kb_id=None)

        return report
