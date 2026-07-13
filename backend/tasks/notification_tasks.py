"""
知识主动推送 Celery 任务 — 定时生成个性化日报和知识缺口预警。

定时调度：
    - daily_personal_digest:  每日 9:00  为所有活跃用户生成知识日报
    - daily_gap_alert:         每日 18:00 知识缺口预警通知管理员
"""

from __future__ import annotations

import asyncio

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


async def _daily_personal_digest() -> dict:
    """为所有活跃用户生成个性化知识日报。"""
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.user import User
    from app.services.notification_service import NotificationService

    async with async_session_factory() as db:
        # 查询所有活跃用户
        result = await db.execute(
            select(User).where(
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        users = result.scalars().all()

        sent_count = 0
        for user in users:
            try:
                service = NotificationService(db)
                recs = await service.generate_personal_digest(str(user.id))
                if recs:
                    sent_count += 1
            except Exception as exc:
                logger.warning(
                    "notification.digest_user_failed",
                    user_id=str(user.id),
                    error=str(exc),
                )

        await db.commit()
        logger.info(
            "notification.daily_digest_completed",
            total_users=len(users),
            sent=sent_count,
        )
        return {"total_users": len(users), "sent": sent_count}


async def _daily_gap_alert() -> dict:
    """知识缺口预警 — 通知知识管理员。"""
    from app.database import async_session_factory
    from app.services.notification_service import NotificationService

    async with async_session_factory() as db:
        service = NotificationService(db)
        notified = await service.send_gap_alert()
        await db.commit()
        logger.info("notification.gap_alert_completed", notified=notified)
        return {"notified": notified}


# ------------------------------------------------------------------
# Celery 任务注册
# ------------------------------------------------------------------

try:
    from celery_app import celery_app

    @celery_app.task(name="tasks.notification_tasks.daily_personal_digest")
    def daily_personal_digest() -> dict:
        """每日 9:00 — 为所有活跃用户生成知识日报。

        推荐逻辑：
        1. 查询所有活跃用户
        2. 对每个用户调用 generate_personal_digest
        3. 通过站内通知记录推送
        """
        logger.info("notification.daily_digest_task_started")
        try:
            result = _run_async(_daily_personal_digest())
            logger.info("notification.daily_digest_task_done", result=result)
            return result
        except Exception as exc:
            logger.error("notification.daily_digest_task_failed", error=str(exc))
            return {"status": "failed", "error": str(exc)}

    @celery_app.task(name="tasks.notification_tasks.daily_gap_alert")
    def daily_gap_alert() -> dict:
        """每日 18:00 — 知识缺口预警。

        基于 gap_detector_service 的缺口检测逻辑，
        通知所有 kb_admin 角色用户。
        """
        logger.info("notification.gap_alert_task_started")
        try:
            result = _run_async(_daily_gap_alert())
            logger.info("notification.gap_alert_task_done", result=result)
            return result
        except Exception as exc:
            logger.error("notification.gap_alert_task_failed", error=str(exc))
            return {"status": "failed", "error": str(exc)}

except ImportError:
    logger.warning("notification.celery_not_available")
