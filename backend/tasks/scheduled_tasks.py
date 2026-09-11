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
- cleanup_stale_checkpoints：每日清理过期 Checkpoint 会话
- generate_quality_report：每周生成质量报告
- cleanup_orphan_multipart_uploads：每日清理 24h 未 complete 的孤儿分片
- rescan_stuck_documents：每小时补偿扫描卡死的文档解析任务并重投
- flush_task_outbox：每 5 分钟补投 task_outbox 欠投递记录（P2 轻量 Outbox）
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
        # 必须重抛：返回 failed dict 会让 Celery 判定任务成功，
        # autoretry 失效、监控无告警，当日检测静默跳过。
        logger.error("scheduled.detect_gaps_failed", error=str(exc))
        raise


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
        raise


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
        raise


@celery_app.task(name="tasks.scheduled_tasks.cleanup_stale_checkpoints")
def cleanup_stale_checkpoints() -> dict[str, Any]:
    """每日清理过期 Checkpoint 会话。

    清理超过 7 天未更新的 agent_checkpoints 记录，
    避免废弃会话的 Checkpoint 无限膨胀。

    Returns:
        清理结果字典，包含清理的会话数量。
    """
    logger.info("scheduled.cleanup_checkpoints_started")
    try:
        result = asyncio.run(_cleanup_stale_checkpoints_async())
        logger.info(
            "scheduled.cleanup_checkpoints_completed",
            cleaned_count=result.get("cleaned_count", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.cleanup_checkpoints_failed", error=str(exc))
        raise


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
        raise


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
        raise


@celery_app.task(name="tasks.scheduled_tasks.cleanup_stale_research")
def cleanup_stale_research() -> dict[str, Any]:
    """每日清理无 TTL 的 Deep Research 快照/结果 Redis 键（P3 兜底）。

    常规路径：``research_progress`` 在每次发布时对快照键重设 TTL，完成后自动过期；
    ``research_result`` 写时带 TTL。本任务作为安全网，清理部署前遗留或异常情况
    下失去 TTL（persistent，ttl == -1）的快照/结果键，防止 Redis 内存累积。

    Returns:
        清理结果字典，含快照与结果的清理数量。
    """
    logger.info("scheduled.cleanup_stale_research_started")
    try:
        result = asyncio.run(_cleanup_stale_research_async())
        logger.info(
            "scheduled.cleanup_stale_research_completed",
            snapshots_cleaned=result.get("snapshots_cleaned", 0),
            results_cleaned=result.get("results_cleaned", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.cleanup_stale_research_failed", error=str(exc))
        raise


@celery_app.task(name="tasks.scheduled_tasks.patrol_external_docs")
def patrol_external_docs() -> dict[str, Any]:
    """每日巡检过期外部文档 — P2 定时兜底安全网。

    捕获 P0（检索时校验）+ P1（webhook）可能遗漏的更新：
        - 从未被检索到的外部文档（P0 没机会检查）
        - webhook 投递失败/未配置的文档（P1 没收到事件）

    方案 A：单一阈值，所有外部文档无类别区分，无盲区。
    last_checked_at 天然限流 — P0/P1 已校验的文档不会重复巡检。

    Returns:
        巡检摘要：total / fresh / updated / failed / skipped。
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.EXTERNAL_SYNC_PATROL_ENABLED:
        logger.info("scheduled.patrol_disabled_by_config")
        return {"total": 0, "skipped": 0, "message": "巡检已禁用"}

    logger.info(
        "scheduled.patrol_started",
        max_staleness_hours=settings.EXTERNAL_SYNC_PATROL_MAX_STALENESS_HOURS,
        batch_size=settings.EXTERNAL_SYNC_PATROL_BATCH_SIZE,
        concurrency=settings.EXTERNAL_SYNC_PATROL_CONCURRENCY,
    )
    try:
        result = asyncio.run(_patrol_external_docs_async(settings))
        logger.info(
            "scheduled.patrol_completed",
            total=result.get("total", 0),
            fresh=result.get("fresh", 0),
            updated=result.get("updated", 0),
            failed=result.get("failed", 0),
            skipped=result.get("skipped", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.patrol_failed", error=str(exc)[:200])
        raise


@celery_app.task(name="tasks.scheduled_tasks.rescan_stuck_documents")
def rescan_stuck_documents() -> dict[str, Any]:
    """每小时补偿扫描卡死的文档解析任务 — P1 兜底安全网。

    背景：即使 broker 走 RabbitMQ（磁盘级持久化），仍有丢消息路径：
    API 侧投递失败未确认、队列被误 purge、任务消费后 DB 写入失败、
    worker 异常退出后消息被 ack 等。此时文档将永远停留在"解析中"
    状态（parse_status 为 NULL/pending，前端进度条无响应且无人重投）。

    卡死判定（同时满足才重投）：
        1. parse_status 为 NULL 或 'pending'（DB 只在 finalize 时写入
           parsed/partial/failed 终态，NULL/pending = 处理从未完成）；
        2. updated_at 超过卡死阈值（默认 2h）无任何 DB 写入；
        3. Redis 解析进度 key 已过期（TTL 30min，key 存活说明 worker
           仍在推进）且任务幂等锁不存在（锁存活说明 worker 仍持有任务）。

    重投安全性：process_document 自带 Redis SETNX 幂等锁（P1-B），
    即使与正在执行的任务竞争，后到者拿锁失败会直接跳过，不会重复处理。

    Returns:
        扫描摘要：candidates / skipped_alive / dispatched / dispatch_failed。
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.DOCUMENT_RESCAN_ENABLED:
        logger.info("scheduled.rescan_stuck_disabled_by_config")
        return {"candidates": 0, "dispatched": 0, "message": "补偿扫描已禁用"}

    logger.info(
        "scheduled.rescan_stuck_started",
        stuck_hours=settings.DOCUMENT_RESCAN_STUCK_HOURS,
        batch_size=settings.DOCUMENT_RESCAN_BATCH_SIZE,
    )
    try:
        result = asyncio.run(_rescan_stuck_documents_async(settings))
        logger.info(
            "scheduled.rescan_stuck_completed",
            candidates=result.get("candidates", 0),
            skipped_alive=result.get("skipped_alive", 0),
            dispatched=result.get("dispatched", 0),
            dispatch_failed=result.get("dispatch_failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.rescan_stuck_failed", error=str(exc)[:200])
        raise


# 轻量 Outbox 补投上限 — 超过视为毒丸（如任务名已废弃/参数非法），标记 dead
# 防止无限重试；dead 记录保留在表中供人工排查。
_OUTBOX_MAX_ATTEMPTS: int = 5
# 单轮补投批量上限 — 派发失败才有记录，正常存量极小
_OUTBOX_BATCH_SIZE: int = 50


@celery_app.task(name="tasks.scheduled_tasks.flush_task_outbox")
def flush_task_outbox() -> dict[str, Any]:
    """每 5 分钟补投 task_outbox 欠投递记录 — P2 轻量 Outbox 兜底。

    背景：一次性触发链路（好评→FAQ 回流、采纳→FAQ 回流、解析→智能处理链、
    FAQ 文档索引、视频→智能处理链）原先"派发失败仅日志"，恢复只能靠
    再次触发。dispatch_with_outbox 在派发失败时以独立短事务写入
    task_outbox 表，本任务定时补投，把恢复的开关从"下一次点击"
    收回到系统内。

    派发语义：按任务名 celery_app.send_task 派发（消息进入 broker 即算
    成功，与任务执行成功解耦 — 与 Outbox 派发语义一致）。

    Returns:
        补投摘要：candidates / dispatched / failed / dead。
    """
    logger.info("scheduled.flush_outbox_started")
    try:
        result = asyncio.run(_flush_task_outbox_async())
        logger.info(
            "scheduled.flush_outbox_completed",
            candidates=result.get("candidates", 0),
            dispatched=result.get("dispatched", 0),
            failed=result.get("failed", 0),
            dead=result.get("dead", 0),
        )
        return result
    except Exception as exc:
        logger.error("scheduled.flush_outbox_failed", error=str(exc)[:200])
        raise


async def _flush_task_outbox_async() -> dict[str, Any]:
    """异步补投 task_outbox pending 记录。

    流程：
    1. 查询 status=pending 且 attempts < 上限的记录（最老的优先）；
    2. 逐条按名字 send_task 派发；
    3. 成功 → status=sent + sent_at；失败 → attempts+1 + last_error，
       达上限 → status=dead。

    每条记录独立短事务更新 — 单条失败不影响其余补投。
    """
    from sqlalchemy import select, update

    from app.database import task_db_session
    from app.models.task_outbox import TaskOutbox

    now = datetime.now(timezone.utc)
    candidates = 0
    dispatched = 0
    failed = 0
    dead = 0

    async with task_db_session() as session:
        stmt = (
            select(TaskOutbox)
            .where(
                TaskOutbox.status == "pending",
                TaskOutbox.attempts < _OUTBOX_MAX_ATTEMPTS,
            )
            .order_by(TaskOutbox.created_at.asc())
            .limit(_OUTBOX_BATCH_SIZE)
        )
        result = await session.execute(stmt)
        entries = list(result.scalars().all())
        candidates = len(entries)

    for entry in entries:
        sent = False
        send_error = ""
        try:
            celery_app.send_task(entry.task_name, kwargs=entry.task_kwargs or {})
            sent = True
        except Exception as exc:
            send_error = str(exc)[:2000]
            logger.warning(
                "scheduled.flush_outbox_redispatch_failed",
                outbox_id=str(entry.id),
                task_name=entry.task_name,
                error=send_error[:200],
            )

        # 独立短事务更新状态 — 单条失败不影响其余
        try:
            async with task_db_session() as session:
                if sent:
                    await session.execute(
                        update(TaskOutbox)
                        .where(TaskOutbox.id == entry.id)
                        .values(
                            status="sent", sent_at=now, last_error=None
                        )
                    )
                else:
                    new_attempts = (entry.attempts or 0) + 1
                    values: dict[str, Any] = {
                        "attempts": new_attempts,
                        "last_error": send_error,
                    }
                    if new_attempts >= _OUTBOX_MAX_ATTEMPTS:
                        values["status"] = "dead"
                        dead += 1
                    await session.execute(
                        update(TaskOutbox).where(TaskOutbox.id == entry.id).values(**values)
                    )
                await session.commit()
        except Exception as upd_exc:
            logger.warning(
                "scheduled.flush_outbox_status_update_failed",
                outbox_id=str(entry.id),
                error=str(upd_exc)[:200],
            )
            # 状态未更新 — attempts 不变，下轮重试；若本已派发成功，
            # 下一轮重复派发由任务自身幂等性兜底
            if sent:
                dispatched += 1
            else:
                failed += 1
            continue

        if sent:
            dispatched += 1
        else:
            failed += 1

    return {
        "status": "success",
        "candidates": candidates,
        "dispatched": dispatched,
        "failed": failed,
        "dead": dead,
        "flushed_at": now.isoformat(),
    }


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


async def _cleanup_stale_checkpoints_async() -> dict[str, Any]:
    """异步清理过期 Checkpoint 会话。"""
    from sqlalchemy import text as sa_text

    from app.database import task_db_session

    stale_days = 7
    async with task_db_session() as session:
        result = await session.execute(
            sa_text(
                "DELETE FROM agent_checkpoints "
                "WHERE updated_at < NOW() - CAST(:interval AS INTERVAL)"
            ),
            {"interval": f"{stale_days} days"},
        )
        cleaned = result.rowcount
        await session.commit()

        return {
            "status": "success",
            "cleaned_count": cleaned,
            "stale_days": stale_days,
            "cleaned_at": datetime.now(timezone.utc).isoformat(),
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


async def _cleanup_stale_research_async() -> dict[str, Any]:
    """删除无 TTL（persistent）的 Deep Research 快照/结果键（P3 兜底）。

    仅处理 ```ttl == -1`` 的键——正常键要么带 TTL（发布时重设 / 写时带 ex），
    要么正被任务持续写入并刷新 TTL，因此不会误删进行中的任务。当前具有 TTL
    的键交由 Redis 自动过期，本任务只清扫部署前遗留或异常失去 TTL 的键。
    """
    from app.config import get_settings

    settings = get_settings()
    snapshots_cleaned = 0
    results_cleaned = 0

    try:
        import redis
        from app.services.research_progress import RESULT_PREFIX, SNAPSHOT_PREFIX

        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        for pattern, counter in (
            (f"{SNAPSHOT_PREFIX}:*", "snapshots"),
            (f"{RESULT_PREFIX}:*", "results"),
        ):
            try:
                for key in client.scan_iter(match=pattern, count=100):
                    try:
                        if client.ttl(key) == -1:
                            client.delete(key)
                            if counter == "snapshots":
                                snapshots_cleaned += 1
                            else:
                                results_cleaned += 1
                    except Exception as exc:
                        logger.warning(
                            "scheduled.cleanup_stale_research_key_failed",
                            key=key,
                            error=str(exc),
                        )
            except Exception as exc:
                logger.warning(
                    "scheduled.cleanup_stale_research_scan_failed",
                    pattern=pattern,
                    error=str(exc),
                )
        client.close()
    except ImportError:
        logger.debug("scheduled.cleanup_stale_research_skipped", reason="redis_not_installed")
    except Exception as exc:
        logger.warning("scheduled.cleanup_stale_research_failed", error=str(exc))

    return {
        "status": "success",
        "snapshots_cleaned": snapshots_cleaned,
        "results_cleaned": results_cleaned,
        "cleaned_at": datetime.now(timezone.utc).isoformat(),
    }


async def _patrol_external_docs_async(settings: Any) -> dict[str, Any]:
    """异步巡检过期外部文档 — 委托 ExternalSyncService.patrol。

    Args:
        settings: 已加载的 Settings 实例（避免重复 IO 读取配置）。
    """
    from app.services.external_sync_service import get_external_sync_service

    service = get_external_sync_service()
    return await service.patrol(
        max_age_hours=settings.EXTERNAL_SYNC_PATROL_MAX_STALENESS_HOURS,
        batch_size=settings.EXTERNAL_SYNC_PATROL_BATCH_SIZE,
        concurrency=settings.EXTERNAL_SYNC_PATROL_CONCURRENCY,
    )


async def _rescan_stuck_documents_async(settings: Any) -> dict[str, Any]:
    """异步补偿扫描卡死的文档解析任务 — 查库 + Redis 存活检查 + 重投。

    Args:
        settings: 已加载的 Settings 实例（避免重复 IO 读取配置）。
    """
    from datetime import timedelta

    import redis as redis_sync
    from sqlalchemy import or_, select

    from app.database import task_db_session
    from app.models.knowledge import Document
    from tasks.document_tasks import _PROGRESS_KEY_PREFIX, process_document

    now = datetime.now(timezone.utc)
    stuck_before = now - timedelta(hours=settings.DOCUMENT_RESCAN_STUCK_HOURS)

    dispatched = 0
    skipped_alive = 0
    dispatch_failed = 0

    # Redis 短连接 — 检查解析进度 key 与任务幂等锁是否存活。
    # Redis 不可用时本轮放弃重投：进度/锁状态未知，无法判定是否卡死
    # （宁漏勿重，下一轮扫描会接力）。
    client = None
    try:
        client = redis_sync.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("scheduled.rescan_redis_connect_failed", error=str(exc)[:200])

    candidates = 0
    async with task_db_session() as session:
        stmt = (
            select(Document)
            .where(
                Document.deleted_at.is_(None),
                # 处理从未完成：NULL（从未写入终态）或 pending
                or_(
                    Document.parse_status.is_(None),
                    Document.parse_status == "pending",
                ),
                # 超过卡死阈值无任何 DB 写入
                Document.updated_at < stuck_before,
            )
            # 最老的优先 — 积压最久的文档最先被补偿
            .order_by(Document.updated_at.asc())
            .limit(settings.DOCUMENT_RESCAN_BATCH_SIZE)
        )
        result = await session.execute(stmt)
        docs = list(result.scalars().all())
        candidates = len(docs)

        for doc in docs:
            doc_id = str(doc.id)

            if client is None:
                break

            try:
                progress_alive = bool(client.exists(f"{_PROGRESS_KEY_PREFIX}{doc_id}"))
                lock_alive = bool(
                    client.exists(
                        f"{settings.TASK_LOCK_REDIS_PREFIX}process_document:{doc_id}"
                    )
                )
            except Exception as exc:
                # Redis 抖动 — 跳过该文档下轮再查（宁漏勿重）
                logger.warning(
                    "scheduled.rescan_check_failed",
                    doc_id=doc_id,
                    error=str(exc)[:200],
                )
                continue

            # 进度或锁仍存活 = worker 还在处理，不是卡死
            if progress_alive or lock_alive:
                skipped_alive += 1
                continue

            try:
                process_document.delay(
                    doc_id,
                    tenant_id=str(doc.tenant_id) if doc.tenant_id else None,
                )
                dispatched += 1
                logger.info(
                    "scheduled.rescan_redispatched",
                    doc_id=doc_id,
                    updated_at=(
                        doc.updated_at.isoformat() if doc.updated_at else None
                    ),
                )
            except Exception as exc:
                dispatch_failed += 1
                logger.warning(
                    "scheduled.rescan_redispatch_failed",
                    doc_id=doc_id,
                    error=str(exc)[:200],
                )

    if client is not None:
        try:
            client.close()
        except Exception:
            pass

    return {
        "status": "success",
        "candidates": candidates,
        "skipped_alive": skipped_alive,
        "dispatched": dispatched,
        "dispatch_failed": dispatch_failed,
        "scanned_at": now.isoformat(),
    }
