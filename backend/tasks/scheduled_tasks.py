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
- cleanup_orphan_multipart_uploads：每日清理 24h 未 complete 的孤儿分片
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


@celery_app.task(name="tasks.scheduled_tasks.cleanup_orphan_multipart_uploads")
def cleanup_orphan_multipart_uploads() -> dict[str, Any]:
    """每日清理 24h 未 complete 的孤儿分片（P1 加固）。

    双策略清理：
    1. 扫描 Redis ``ekb:multipart:*`` 键 — created_at 超过 12h 的视为停滞上传，
       调用 ``abort_multipart_upload`` 清理 MinIO 分片 + 删除 Redis key；
    2. 扫描 MinIO ``list_multipart_uploads`` — initiated 超过 24h 的视为孤儿
       （Redis TTL 已过期，元数据丢失），调用 abort 释放存储空间。

    Returns:
        清理结果字典，包含两个策略各自清理的数量。
    """
    logger.info("scheduled.cleanup_multipart_started")
    try:
        result = asyncio.run(_cleanup_orphan_multipart_uploads_async())
        logger.info(
            "scheduled.cleanup_multipart_completed",
            redis_cleaned=result.get("redis_cleaned", 0),
            minio_cleaned=result.get("minio_cleaned", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.cleanup_multipart_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ------------------------------------------------------------------
# 异步实现
# ------------------------------------------------------------------

async def _detect_gaps_async() -> dict[str, Any]:
    """异步检测知识缺口。"""
    from sqlalchemy import select

    from app.database import task_db_session
    from app.models.billing import Tenant
    from app.services.gap_detector_service import GapDetectorService

    async with task_db_session() as session:
        # 按租户迭代：逐租户创建带 tenant_id 的 GapDetectorService，确保多租户隔离
        tenants_result = await session.execute(
            select(Tenant).where(Tenant.deleted_at.is_(None))
        )
        tenants = list(tenants_result.scalars().all())

        total_gaps = 0
        high_freq_gaps = 0
        for tenant in tenants:
            service = GapDetectorService(session, tenant_id=tenant.id)
            gaps = await service.detect_gaps()

            # 获取全部 open 缺口用于统计
            all_gaps = await service.get_gaps()
            high_freq_gaps += len(gaps)
            total_gaps += len(all_gaps)

        return {
            "status": "success",
            "total_gaps": total_gaps,
            "high_frequency_gaps": high_freq_gaps,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }


async def _check_expiration_async() -> dict[str, Any]:
    """异步检查知识过期预警 — 调用 Graphiti 时间线。"""
    from app.database import task_db_session
    from app.models.billing import Tenant
    from app.models.memory import KnowledgeEntity
    from app.utils.tenant import apply_tenant_filter
    from sqlalchemy import select
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    threshold = now + timedelta(days=7)

    async with task_db_session() as session:
        # 按租户迭代：逐租户查询即将过期的知识实体，确保多租户隔离
        tenants_result = await session.execute(
            select(Tenant).where(Tenant.deleted_at.is_(None))
        )
        tenants = list(tenants_result.scalars().all())

        expiring_list: list[dict[str, Any]] = []
        for tenant in tenants:
            # 查询即将过期（valid_to 在未来 7 天内且非 NULL）的知识实体
            stmt = (
                select(KnowledgeEntity)
                .where(
                    KnowledgeEntity.valid_to.isnot(None),
                    KnowledgeEntity.valid_to <= threshold,
                    KnowledgeEntity.valid_to >= now,
                )
            )
            stmt = apply_tenant_filter(stmt, KnowledgeEntity, tenant.id)
            result = await session.execute(stmt)
            for e in result.scalars().all():
                expiring_list.append({
                    "id": str(e.id),
                    "name": e.name,
                    "entity_type": e.entity_type,
                    "valid_from": e.valid_from.isoformat() if e.valid_from else None,
                    "valid_to": e.valid_to.isoformat() if e.valid_to else None,
                })

        await session.commit()

        return {
            "status": "success",
            "expiring_count": len(expiring_list),
            "expiring_entities": expiring_list,
            "checked_at": now.isoformat(),
        }


async def _cleanup_expired_facts_async() -> dict[str, Any]:
    """异步清理过期的记忆事实。"""
    from app.database import task_db_session
    from app.models.billing import Tenant
    from app.models.memory import MemoryFact
    from app.utils.tenant import apply_tenant_filter
    from sqlalchemy import select
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    async with task_db_session() as session:
        # 按租户迭代：逐租户查询并清理过期的记忆事实，确保多租户隔离
        tenants_result = await session.execute(
            select(Tenant).where(Tenant.deleted_at.is_(None))
        )
        tenants = list(tenants_result.scalars().all())

        cleaned_count = 0
        for tenant in tenants:
            # 查询已过期但仍然 active 的事实
            stmt = select(MemoryFact).where(
                MemoryFact.expires_at.isnot(None),
                MemoryFact.expires_at < now,
                MemoryFact.is_active.is_(True),
            )
            stmt = apply_tenant_filter(stmt, MemoryFact, tenant.id)
            result = await session.execute(stmt)
            for fact in result.scalars().all():
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
    from sqlalchemy import select

    from app.database import task_db_session
    from app.models.billing import Tenant
    from app.services.quality_service import QualityService

    async with task_db_session() as session:
        # 按租户迭代：逐租户创建带 tenant_id 的 QualityService，确保多租户隔离
        tenants_result = await session.execute(
            select(Tenant).where(Tenant.deleted_at.is_(None))
        )
        tenants = list(tenants_result.scalars().all())

        merged_report: dict[str, Any] = {
            "status": "success",
            "total_docs": 0,
            "average_score": 0,
            "low_quality_count": 0,
            "tenant_reports": [],
        }
        total_score = 0
        for tenant in tenants:
            service = QualityService(session, tenant_id=tenant.id)
            report = await service.get_quality_report(kb_id=None)
            merged_report["total_docs"] += report.get("total_docs", 0)
            merged_report["low_quality_count"] += report.get("low_quality_count", 0)
            total_score += report.get("average_score", 0)
            merged_report["tenant_reports"].append(report)

        if tenants:
            merged_report["average_score"] = total_score / len(tenants)
        return merged_report


async def _cleanup_orphan_multipart_uploads_async() -> dict[str, Any]:
    """异步清理孤儿分片 — Redis + MinIO 双策略（P1 加固）。

    策略 1：扫描 Redis ``ekb:multipart:*`` 键，created_at 超过 12h 的视为停滞上传，
    调用 ``abort_multipart_upload`` 清理 MinIO 分片 + 删除 Redis key。

    策略 2：扫描 MinIO ``list_multipart_uploads``，initiated 超过 24h 的视为孤儿
    （Redis TTL 已过期，元数据丢失），调用 abort 释放存储空间。
    跳过策略 1 已清理的 upload_id，避免重复操作。
    """
    import json
    import time
    from datetime import datetime, timezone

    from app.config import get_settings

    settings = get_settings()
    now = time.time()
    redis_threshold = 12 * 3600   # 12h — Redis 停滞阈值
    minio_threshold = 24 * 3600   # 24h — MinIO 孤儿阈值

    redis_cleaned = 0
    minio_cleaned = 0
    cleaned_upload_ids: set[str] = set()

    # ------------------------------------------------------------------
    # 策略 1: 扫描 Redis ekb:multipart:* 键
    # ------------------------------------------------------------------
    try:
        import redis

        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        for key in client.scan_iter(match="ekb:multipart:*", count=100):
            try:
                raw = client.get(key)
                if not raw:
                    continue
                session = json.loads(raw)
                created_at = session.get("created_at", 0)
                minio_upload_id = session.get("minio_upload_id", "")
                object_name = session.get("object_name", "")

                # 未超过 12h 阈值的跳过（上传可能仍在进行）
                if now - created_at < redis_threshold:
                    continue

                # 调用 abort 清理 MinIO 分片
                if minio_upload_id and object_name:
                    try:
                        from app.utils.minio_client import abort_multipart_upload

                        await abort_multipart_upload(
                            bucket="ekb-documents",
                            object_name=object_name,
                            upload_id=minio_upload_id,
                        )
                        cleaned_upload_ids.add(minio_upload_id)
                        redis_cleaned += 1
                    except Exception as exc:
                        logger.warning(
                            "scheduled.cleanup_abort_failed",
                            upload_id=minio_upload_id,
                            error=str(exc),
                        )

                # 删除 Redis key
                client.delete(key)
            except Exception as exc:
                logger.warning(
                    "scheduled.cleanup_key_failed",
                    key=key,
                    error=str(exc),
                )
        client.close()
    except ImportError:
        logger.debug("scheduled.cleanup_redis_skipped", reason="redis_not_installed")
    except Exception as exc:
        logger.warning("scheduled.cleanup_redis_failed", error=str(exc))

    # ------------------------------------------------------------------
    # 策略 2: 扫描 MinIO list_multipart_uploads（兜底孤儿）
    # ------------------------------------------------------------------
    try:
        from app.utils.minio_client import list_multipart_uploads, abort_multipart_upload

        uploads = await list_multipart_uploads(bucket="ekb-documents")
        for u in uploads:
            upload_id = u.get("upload_id", "")
            object_name = u.get("object_name", "")
            initiated = u.get("initiated")

            # 跳过策略 1 已清理的
            if upload_id in cleaned_upload_ids:
                continue

            # 解析 initiated 时间（datetime 或 ISO 字符串）
            if isinstance(initiated, datetime):
                initiated_ts = initiated.replace(tzinfo=timezone.utc).timestamp()
            elif isinstance(initiated, str):
                try:
                    initiated_ts = datetime.fromisoformat(
                        initiated.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    continue
            else:
                continue

            # 未超过 24h 阈值的跳过
            if now - initiated_ts < minio_threshold:
                continue

            try:
                await abort_multipart_upload(
                    bucket="ekb-documents",
                    object_name=object_name,
                    upload_id=upload_id,
                )
                minio_cleaned += 1
            except Exception as exc:
                logger.warning(
                    "scheduled.cleanup_minio_abort_failed",
                    upload_id=upload_id,
                    error=str(exc),
                )
    except ImportError:
        logger.debug("scheduled.cleanup_minio_skipped", reason="minio_not_installed")
    except Exception as exc:
        logger.warning("scheduled.cleanup_minio_failed", error=str(exc))

    return {
        "status": "success",
        "redis_cleaned": redis_cleaned,
        "minio_cleaned": minio_cleaned,
        "cleaned_at": datetime.now(timezone.utc).isoformat(),
    }
