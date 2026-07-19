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
    queues=["documents", "indexing", "scheduled", "notifications", "multimodal"],
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

    # 重试配置
    task_default_retry_delay=60,  # 默认重试间隔 60 秒
    task_max_retries=3,           # 最多重试 3 次

    # 结果过期时间 — 1 小时后自动清理
    result_expires=3600,

    # 并发配置
    worker_concurrency=4,
)

# ------------------------------------------------------------------
# 定时任务调度（Celery Beat）
# ------------------------------------------------------------------

celery_app.conf.beat_schedule = {
    # 每日检测高频无结果查询（知识缺口）
    "detect-knowledge-gaps-daily": {
        "task": "tasks.scheduled_tasks.detect_knowledge_gaps",
        "schedule": "0 2 * * *",  # 每天凌晨 2 点
    },
    # 每日检查知识过期预警
    "check-expiration-daily": {
        "task": "tasks.scheduled_tasks.check_expiration",
        "schedule": "0 3 * * *",  # 每天凌晨 3 点
    },
    # 每日清理过期记忆事实
    "cleanup-expired-facts-daily": {
        "task": "tasks.scheduled_tasks.cleanup_expired_facts",
        "schedule": "0 4 * * *",  # 每天凌晨 4 点
    },
    # 每周一生成质量报告
    "generate-quality-report-weekly": {
        "task": "tasks.scheduled_tasks.generate_quality_report",
        "schedule": "0 8 * * 1",  # 每周一早上 8 点
    },
    # 每日 9:00 — 个性化知识日报
    "daily-personal-digest": {
        "task": "tasks.notification_tasks.daily_personal_digest",
        "schedule": "0 9 * * *",  # 每天早上 9 点
    },
    # 每日 18:00 — 知识缺口预警
    "daily-gap-alert": {
        "task": "tasks.notification_tasks.daily_gap_alert",
        "schedule": "0 18 * * *",  # 每天下午 6 点
    },
}

logger.info(
    "celery.app_configured",
    broker=settings.REDIS_URL,
    queues=["documents", "indexing", "scheduled"],
)
