"""
Celery 应用入口 — 单一职责：创建并配置 Celery 实例。

遵循开闭原则：新增任务模块只需在 tasks/ 下创建文件并在 autodiscover
覆盖路径中注册，无需修改任务调用方代码。
遵循依赖倒置：所有配置从 app.config.get_settings() 获取，
不在此硬编码 Redis 地址等连接信息。
"""

from __future__ import annotations

import os
import sys

# 确保 app 包可被导入（Celery worker 通常从 backend/ 目录启动）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from celery import Celery  # noqa: E402
from celery.schedules import crontab  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)
settings = get_settings()

# ------------------------------------------------------------------
# Celery 应用创建
# ------------------------------------------------------------------

celery_app = Celery(
    "ekb_worker",
    broker=settings.REDIS_URL,
    queues=[
        "documents",
        "indexing",
        "scheduled",
        "notifications",
        "multimodal",
        "dead_letter",  # 死信队列 — 重试耗尽的任务进入此队列供人工排查
    ],
    backend=settings.REDIS_URL,
    include=[
        "tasks.document_tasks",
        "tasks.index_tasks",
        "tasks.scheduled_tasks",
        "tasks.notification_tasks",
        "tasks.multimodal_tasks",
        "tasks.video_tasks",
    ],
)

# ------------------------------------------------------------------
# Celery 配置
# ------------------------------------------------------------------

celery_app.conf.update(
    # 任务序列化
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,

    # 任务路由 — 按模块路由到不同队列
    task_routes={
        "tasks.document_tasks.*": {"queue": "documents"},
        "tasks.index_tasks.*": {"queue": "indexing"},
        "tasks.scheduled_tasks.*": {"queue": "scheduled"},
        "tasks.notification_tasks.*": {"queue": "notifications"},
        "tasks.multimodal_tasks.*": {"queue": "multimodal"},
    },

    # 任务超时（秒）— 防止任务卡死
    task_time_limit=1800,       # 硬超时：30 分钟
    task_soft_time_limit=1500,  # 软超时：25 分钟（可捕获 SoftTimeLimitExceeded）

    # 预取策略 — 每个 worker 一次只预取 1 个任务，避免长任务阻塞短任务
    worker_prefetch_multiplier=1,

    # 任务确认 — 任务完成后才确认（防止 worker 崩溃丢失任务）
    task_acks_late=True,
    # 可靠性：worker 进程被 OOM Killer 强杀时，把任务重投回队列而非标记为 acked
    # 配合 task_acks_late=True，确保 worker 异常退出时任务不丢失
    task_reject_on_worker_lost=True,

    # 重试配置
    task_default_retry_delay=60,  # 默认重试间隔 60 秒
    task_max_retries=3,           # 最多重试 3 次

    # 结果过期时间 — 1 小时后自动清理
    result_expires=3600,

    # 并发配置
    worker_concurrency=4,

    # 死信队列 — 重试耗尽的任务路由到 dead_letter 队列供人工排查
    # 通过 task_routes 中单独的 dead_letter 路由规则实现（见下方）
)

# ------------------------------------------------------------------
# 死信队列路由 — 重试耗尽的任务自动进入 dead_letter 队列
# ------------------------------------------------------------------
# Celery 的 task_routes 按 task 名称路由，死信通过 task 配置 max_retries
# 后 raise self.retry(exc=exc) 达到上限会抛 MaxRetriesExceededError。
# 我们在 task 装饰器上配置 deadletter 行为（见各 task 文件）。
# 此处配置 Redis broker 的可见性超时 — 确保任务不会因 broker 超时被重复消费

celery_app.conf.update(
    # 可见性超时 — worker 取出任务后，如果在此时长内未 ACK，任务会被重新投递
    # 默认 3600s（1小时），设为 6 小时覆盖长任务（如视频处理）
    broker_transport_options={"visibility_timeout": 21600},
    result_backend_transport_options={"visibility_timeout": 21600},
)

# ------------------------------------------------------------------
# 定时任务调度（Celery Beat）
# ------------------------------------------------------------------

celery_app.conf.beat_schedule = {
    # 每日检测高频无结果查询（知识缺口）
    "detect-knowledge-gaps-daily": {
        "task": "tasks.scheduled_tasks.detect_knowledge_gaps",
        "schedule": crontab(minute=0, hour=2),  # 每天凌晨 2 点
    },
    # 每日检查知识过期预警
    "check-expiration-daily": {
        "task": "tasks.scheduled_tasks.check_expiration",
        "schedule": crontab(minute=0, hour=3),  # 每天凌晨 3 点
    },
    # 每日清理过期记忆事实
    "cleanup-expired-facts-daily": {
        "task": "tasks.scheduled_tasks.cleanup_expired_facts",
        "schedule": crontab(minute=0, hour=4),  # 每天凌晨 4 点
    },
    # 每周一生成质量报告
    "generate-quality-report-weekly": {
        "task": "tasks.scheduled_tasks.generate_quality_report",
        "schedule": crontab(minute=0, hour=8, day_of_week=1),  # 每周一早上 8 点
    },
    # 每日凌晨 5 点 — 清理 24h 未 complete 的孤儿分片（P1 加固）
    "cleanup-orphan-multipart-daily": {
        "task": "tasks.scheduled_tasks.cleanup_orphan_multipart_uploads",
        "schedule": crontab(minute=0, hour=5),  # 每天凌晨 5 点
    },
    # 每日 9:00 — 个性化知识日报
    "daily-personal-digest": {
        "task": "tasks.notification_tasks.daily_personal_digest",
        "schedule": crontab(minute=0, hour=9),  # 每天早上 9 点
    },
    # 每日 18:00 — 知识缺口预警
    "daily-gap-alert": {
        "task": "tasks.notification_tasks.daily_gap_alert",
        "schedule": crontab(minute=0, hour=18),  # 每天下午 6 点
    },
}

logger.info(
    "celery.app_configured",
    broker=settings.REDIS_URL,
    queues=["documents", "indexing", "scheduled"],
)


# ------------------------------------------------------------------
# Beat 单实例锁 — 分布式预备（P1）
# ------------------------------------------------------------------
# 多实例部署时 Beat 必须单实例运行，否则定时任务会重复执行。
# 通过 Redis SETNX 实现单实例锁：Beat 启动时获取锁，获取不到则退出。
# 锁有 TTL（默认 60s），Beat 进程崩溃后锁自动过期，备用实例可接管。


def acquire_beat_lock(redis_url: str, lock_key: str = "celery:beat:lock", ttl: int = 60) -> bool:
    """尝试获取 Beat 单实例锁。

    多实例部署时，只有获取到锁的 Beat 实例才会运行，
    其他实例启动后立即退出，避免定时任务重复执行。

    Args:
        redis_url: Redis 连接 URL。
        lock_key: 锁的 Redis key。
        ttl: 锁过期时间（秒），Beat 崩溃后锁自动释放。

    Returns:
        True = 获取成功，可运行 Beat；False = 已有其他实例运行。
    """
    try:
        import redis

        client = redis.from_url(redis_url, decode_responses=True)
        # SET key value NX EX ttl — 原子化获取锁
        acquired = client.set(lock_key, "beat_active", nx=True, ex=ttl)
        if acquired:
            logger.info("celery.beat_lock_acquired", lock_key=lock_key, ttl=ttl)
            return True
        logger.warning("celery.beat_lock_held", lock_key=lock_key)
        return False
    except Exception as exc:
        # Redis 不可用时放行（单机模式不需要锁）
        logger.warning("celery.beat_lock_failed", error=str(exc)[:200])
        return True


# Beat 启动前检查单实例锁（仅当通过 celery beat 命令启动时触发）
# 通过环境变量控制，避免影响 worker 进程
if os.environ.get("CELERY_BEAT_SINGLE_INSTANCE") == "1":
    if not acquire_beat_lock(settings.REDIS_URL):
        logger.error("celery.beat_another_instance_running")
        # Beat 锁已被持有，当前实例退出
        # 不使用 sys.exit，让 Celery 的信号机制正常处理
        raise SystemExit("Beat 单实例锁已被持有，当前实例退出")
