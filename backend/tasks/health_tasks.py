"""AI 服务健康检查 Celery 定时任务。

P2-A Task 2: 每 30 秒执行一次，检查所有 Provider 可用性。

幂等保障：
- Redis SETNX 锁（key=lock:health_check, TTL=60s）防止并行执行
- 锁未获取时跳过，输出 skipped 日志
- 健康检查不修改任何状态，天然幂等

调度配置：celery_app.py beat_schedule → "health-check-providers-30s"
"""

from __future__ import annotations

import asyncio

from app.utils.logger import get_logger

log = get_logger(__name__)


async def _run_health_check() -> dict:
    """执行健康检查 — 带 Redis 锁防止并行。"""
    from app.services.health_check import HealthCheckService
    from app.utils.task_lock import TaskLockContext

    async with TaskLockContext("health_check", "all", ttl=60) as lock:
        if not lock.acquired:
            log.info("health_check_task.skipped", reason="already_running")
            return {"status": "skipped", "reason": "already_running"}

        log.info("health_check_task.start")
        service = HealthCheckService()
        results = await service.check_all()
        healthy = sum(1 for h in results.values() if h.healthy)
        log.info(
            "health_check_task.complete",
            total=len(results),
            healthy=healthy,
            unhealthy=len(results) - healthy,
        )
        return {
            "status": "completed",
            "total": len(results),
            "healthy": healthy,
            "unhealthy": len(results) - healthy,
        }


def health_check_all_providers() -> dict:
    """Celery 任务入口 — 健康检查所有 AI 服务 Provider。

    幂等：Redis SETNX 锁确保同一时刻仅一个实例运行。
    """
    try:
        return asyncio.run(_run_health_check())
    except Exception as exc:
        log.error("health_check_task.error", error=str(exc))
        return {"status": "error", "error": str(exc)}


# Celery 任务注册 — 延迟导入避免循环依赖
try:
    from celery_app import celery_app

    @celery_app.task(name="tasks.health_tasks.health_check_all_providers")
    def _health_check_task():
        """Celery registered task wrapper."""
        return health_check_all_providers()

except Exception:
    pass
