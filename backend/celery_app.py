"""
Celery 应用入口 — 单一职责：创建并配置 Celery 实例。

遵循开闭原则：新增任务模块只需在 tasks/ 下创建文件并在 autodiscover
覆盖路径中注册，无需修改任务调用方代码。
遵循依赖倒置：所有配置从 app.config.get_settings() 获取，
不在此硬编码 Redis 地址等连接信息。
"""

from __future__ import annotations

import os
import socket
import sys
import threading

# 确保 app 包可被导入（Celery worker 通常从 backend/ 目录启动）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from celery import Celery  # noqa: E402
from celery.schedules import crontab  # noqa: E402
from celery.signals import worker_shutdown  # noqa: E402
from kombu import Queue  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)
settings = get_settings()

# 全部业务队列清单 — task_queues 与 task_routes 的唯一事实来源。
# 包含默认队列 "celery" 作为兜底：任何未匹配 task_routes 的任务
# 落入默认队列后仍会被 worker 消费，避免静默堆积。
_ALL_QUEUES: tuple[str, ...] = (
    "celery",        # 默认队列（兜底）
    "documents",     # 文档解析/智能处理/测试平台 LLM 任务
    "indexing",      # 索引构建
    "scheduled",     # 定时任务/健康检查
    "notifications", # 通知推送
    "multimodal",    # 多模态/音视频处理
    "dead_letter",   # 死信队列 — 重试耗尽的任务进入此队列供人工排查
)

# ------------------------------------------------------------------
# Celery 应用创建
# ------------------------------------------------------------------

celery_app = Celery(
    "ekb_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "tasks.document_tasks",
        "tasks.index_tasks",
        "tasks.scheduled_tasks",
        "tasks.notification_tasks",
        "tasks.multimodal_tasks",
        "tasks.video_tasks",
        "tasks.intelligence_tasks",
        "tasks.compounding_tasks",
        "tasks.testing_tasks",
        "tasks.health_tasks",
    ],
)

# ------------------------------------------------------------------
# Celery 配置
# ------------------------------------------------------------------

celery_app.conf.update(
    # 队列声明 — 修复：原实现把 queues=[...] 传给 Celery() 构造函数，
    # 该参数不是有效配置键（仅存入 preconf 不映射为 task_queues），
    # 导致 task_queues 为空、worker 只消费默认 "celery" 队列，
    # 路由到命名队列的任务全部堆积无人消费。
    task_queues=tuple(Queue(name) for name in _ALL_QUEUES),
    task_default_queue="celery",

    # 任务序列化
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,

    # 任务路由 — 按模块路由到不同队列（覆盖全部任务模块，
    # 未匹配的落入默认 "celery" 队列兜底）
    task_routes={
        "tasks.document_tasks.*": {"queue": "documents"},
        "tasks.index_tasks.*": {"queue": "indexing"},
        "tasks.scheduled_tasks.*": {"queue": "scheduled"},
        "tasks.notification_tasks.*": {"queue": "notifications"},
        "tasks.multimodal_tasks.*": {"queue": "multimodal"},
        # 视频分片处理（GB 级，ASR 相关）— 归入多模态队列，避免阻塞文档解析
        "tasks.video_tasks.*": {"queue": "multimodal"},
        # 文档智能处理（摘要/标签/分类）— 文档解析的链式后续
        "tasks.intelligence_tasks.*": {"queue": "documents"},
        # 知识回流（提取/冲突检测/复用注入）— LLM 异步任务
        "tasks.compounding_tasks.*": {"queue": "documents"},
        # 智能测试平台（需求提取/用例生成/计划编排）— LLM 异步任务
        "tasks.testing_tasks.*": {"queue": "documents"},
        # AI 服务健康检查（每 30s，轻量高频）— 归入定时队列
        "tasks.health_tasks.*": {"queue": "scheduled"},
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
# 优雅关闭 — worker_shutdown 信号处理
# ------------------------------------------------------------------
# 当 Celery worker 收到 SIGTERM/SIGINT 时，触发 worker_shutdown 信号。
# 在此清理数据库连接池、Redis 连接等资源，确保 worker 退出时不泄漏连接。

@worker_shutdown.connect
def _on_worker_shutdown(**kwargs) -> None:
    """Celery worker 关闭时清理资源。"""
    import asyncio

    logger.info("celery.worker_shutdown_start")

    try:
        from app.database import engine

        # engine.dispose 是 async，在 sync 信号处理器中用事件循环
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(engine.dispose())
            loop.close()
        except Exception:
            pass  # 连接池可能已关闭
        logger.info("celery.worker_shutdown_pg_disposed")
    except Exception as exc:
        logger.warning("celery.worker_shutdown_cleanup_failed", error=str(exc)[:200])

    logger.info("celery.worker_shutdown_done")

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
    # 每日清理过期 Checkpoint 会话
    "cleanup-stale-checkpoints-daily": {
        "task": "tasks.scheduled_tasks.cleanup_stale_checkpoints",
        "schedule": crontab(minute=30, hour=4),  # 每天凌晨 4:30
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
    # 每 30 秒 — AI 服务健康检查（P2-A）
    "health-check-providers-30s": {
        "task": "tasks.health_tasks.health_check_all_providers",
        "schedule": 30.0,  # 每 30 秒
    },
}

logger.info(
    "celery.app_configured",
    broker=settings.REDIS_URL,
    queues=list(_ALL_QUEUES),
)


# ------------------------------------------------------------------
# Beat 单实例锁 — 分布式预备（P1）
# ------------------------------------------------------------------
# 多实例部署时 Beat 必须单实例运行，否则定时任务会重复执行。
# 通过 Redis SETNX 实现单实例锁：Beat 启动时获取锁，获取不到则退出。
# 锁有 TTL（默认 60s），Beat 进程崩溃后锁自动过期，备用实例可接管。


def acquire_beat_lock(
    redis_url: str,
    lock_key: str = "celery:beat:lock",
    ttl: int = 60,
    lock_value: str = "beat_active",
) -> bool:
    """尝试获取 Beat 单实例锁。

    多实例部署时，只有获取到锁的 Beat 实例才会运行，
    其他实例启动后立即退出，避免定时任务重复执行。

    Args:
        redis_url: Redis 连接 URL。
        lock_key: 锁的 Redis key。
        ttl: 锁过期时间（秒），Beat 崩溃后锁自动释放。
        lock_value: 锁的唯一标识值，续期/重取只作用于持有该值的锁。

    Returns:
        True = 获取成功，可运行 Beat；False = 已有其他实例运行。
    """
    try:
        import redis

        client = redis.from_url(redis_url, decode_responses=True)
        # SET key value NX EX ttl — 原子化获取锁
        acquired = client.set(lock_key, lock_value, nx=True, ex=ttl)
        if acquired:
            logger.info("celery.beat_lock_acquired", lock_key=lock_key, ttl=ttl)
            return True
        logger.warning("celery.beat_lock_held", lock_key=lock_key)
        return False
    except Exception as exc:
        # Redis 不可用时放行（单机模式不需要锁）
        logger.warning("celery.beat_lock_failed", error=str(exc)[:200])
        return True


# Lua 脚本 — 原子化续期锁（仅当 value 匹配时才刷新 TTL，防止延长他人持有的锁）
_BEAT_LOCK_RENEW_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("set", KEYS[1], ARGV[1], "EX", ARGV[2])
else
    return 0
end
"""


def start_beat_lock_renewal(
    redis_url: str,
    lock_key: str = "celery:beat:lock",
    lock_value: str = "beat_active",
    ttl: int = 60,
    interval: float | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """启动 Beat 锁续期守护线程 — 运行期间定期刷新 TTL，防止锁过期后双 Beat 并发。

    修复：原实现锁 TTL 60s 但从不续期，Beat 运行 60s 后锁自动过期，
    其他 Beat 实例可再次获取锁 → 双 Beat 并发、定时任务重复执行。

    Args:
        redis_url: Redis 连接 URL。
        lock_key: 锁的 Redis key。
        lock_value: 锁的唯一标识值（续期/重取只作用于本实例持有的锁）。
        ttl: 锁过期时间（秒），每次续期重置为该值。
        interval: 续期周期间隔（秒），默认 ttl // 3（至少 1 秒）。
        stop_event: 停止信号（可选），设置后线程退出。

    Returns:
        (续期线程, 停止事件)；线程为 daemon，进程退出时自动结束。
    """
    interval = interval if interval is not None else max(ttl // 3, 1)
    stop_event = stop_event or threading.Event()

    def _renew_loop() -> None:
        import redis

        client = None
        try:
            while not stop_event.wait(interval):
                try:
                    if client is None:
                        client = redis.from_url(redis_url, decode_responses=True)
                    renewed = client.eval(
                        _BEAT_LOCK_RENEW_SCRIPT, 1, lock_key, lock_value, str(ttl)
                    )
                    if renewed:
                        logger.debug("celery.beat_lock_renewed", lock_key=lock_key, ttl=ttl)
                        continue
                    # 锁已丢失（过期或被接管）— 仅当无人持有时才重新获取
                    reacquired = client.set(lock_key, lock_value, nx=True, ex=ttl)
                    if reacquired:
                        logger.warning("celery.beat_lock_reacquired", lock_key=lock_key)
                    else:
                        logger.error("celery.beat_lock_lost", lock_key=lock_key)
                        break
                except Exception as exc:
                    # Redis 抖动时下个周期重试（重建连接）
                    logger.warning("celery.beat_lock_renew_failed", error=str(exc)[:200])
                    client = None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    thread = threading.Thread(target=_renew_loop, name="beat-lock-renewal", daemon=True)
    thread.start()
    logger.info("celery.beat_lock_renewal_started", lock_key=lock_key, interval=interval)
    return thread, stop_event


# Beat 启动前检查单实例锁（仅当通过 celery beat 命令启动时触发）
# 通过环境变量控制，避免影响 worker 进程
if os.environ.get("CELERY_BEAT_SINGLE_INSTANCE") == "1":
    # 锁值携带主机名与 PID，确保续期/重取只作用于本实例持有的锁
    _beat_lock_value = f"beat_active:{socket.gethostname()}:{os.getpid()}"
    if not acquire_beat_lock(settings.REDIS_URL, lock_value=_beat_lock_value):
        logger.error("celery.beat_another_instance_running")
        # Beat 锁已被持有，当前实例退出
        # 不使用 sys.exit，让 Celery 的信号机制正常处理
        raise SystemExit("Beat 单实例锁已被持有，当前实例退出")
    # 修复：锁 TTL 60s 但从不续期会导致锁过期、双 Beat 并发，
    # 启动后台守护线程在 Beat 运行期间定期续期（默认 ttl//3 周期）
    start_beat_lock_renewal(settings.REDIS_URL, lock_value=_beat_lock_value)
