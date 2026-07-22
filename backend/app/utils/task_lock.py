"""
任务幂等锁 — 基于 Redis SETNX 实现分布式任务锁。

核心能力：
    1. acquire_task_lock — 获取任务锁（SETNX + TTL）
    2. release_task_lock — 释放任务锁（Lua 脚本保证原子性）
    3. TaskLockContext — 上下文管理器，自动获取/释放锁

幂等性保证：
    同一任务（相同 lock_key）在锁 TTL 内只能被一个 worker 执行。
    其他 worker 尝试获取锁失败时直接跳过，避免重复处理。

遵循单一职责：仅提供锁的获取/释放逻辑，不涉及业务判断。
遵循依赖倒置：TTL 从 app.config.get_settings() 获取。

使用方式（上下文管理器）::

    from app.utils.task_lock import TaskLockContext

    async with TaskLockContext("process_document:doc-123") as lock:
        if not lock.acquired:
            log.info("task_already_running", key=lock.key)
            return  # 已有其他 worker 在处理
        # 执行任务逻辑...

使用方式（手动）::

    from app.utils.task_lock import acquire_task_lock, release_task_lock

    lock = await acquire_task_lock("process_document:doc-123")
    if not lock.acquired:
        return  # 跳过
    try:
        await do_work()
    finally:
        await release_task_lock(lock)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)


# Lua 脚本 — 原子化释放锁（仅当 value 匹配时才删除，防止误删他人持有的锁）
_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@dataclass
class TaskLock:
    """任务锁状态。

    Attributes:
        key: Redis key（如 "lock:task:process_document:doc-123"）。
        value: 锁的唯一标识（UUID），用于安全释放。
        acquired: 是否成功获取锁。
        ttl: 锁的 TTL（秒）。
    """

    key: str
    value: str
    acquired: bool
    ttl: int


async def _get_redis():
    """延迟导入 Redis 连接，避免循环依赖。"""
    import redis.asyncio as aioredis

    settings = get_settings()
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def acquire_task_lock(
    task_name: str,
    task_id: str,
    ttl: int | None = None,
) -> TaskLock:
    """获取任务幂等锁（Redis SETNX）。

    使用 SET key value NX EX ttl 原子化获取锁。
    同一 task_name + task_id 在 TTL 内只能被一个 worker 获取。

    Args:
        task_name: 任务名称（如 "process_document", "build_index"）。
        task_id: 任务目标 ID（如 doc_id, kb_id）。
        ttl: 锁过期时间（秒），默认 settings.TASK_LOCK_TTL。

    Returns:
        TaskLock 实例，acquired=True 表示获取成功。
    """
    settings = get_settings()
    _ttl = ttl or settings.TASK_LOCK_TTL
    prefix = settings.TASK_LOCK_REDIS_PREFIX

    key = f"{prefix}{task_name}:{task_id}"
    value = str(uuid.uuid4())

    try:
        redis = await _get_redis()
        try:
            result = await redis.set(key, value, nx=True, ex=_ttl)
            acquired = result is not None

            if acquired:
                log.info("task_lock.acquired", key=key, ttl=_ttl)
            else:
                log.info("task_lock.held", key=key)

            return TaskLock(key=key, value=value, acquired=acquired, ttl=_ttl)
        finally:
            await redis.close()
    except Exception as exc:
        # Redis 不可用时放行（降级为无锁模式，单机场景安全）
        log.warning("task_lock.redis_unavailable", error=str(exc)[:200])
        return TaskLock(key=key, value=value, acquired=True, ttl=_ttl)


async def release_task_lock(lock: TaskLock) -> bool:
    """释放任务锁（Lua 脚本保证原子性）。

    仅当锁的 value 匹配时才删除，防止误删他人持有的锁。

    Args:
        lock: acquire_task_lock 返回的 TaskLock 实例。

    Returns:
        True = 释放成功，False = 锁已被他人持有或已过期。
    """
    if not lock.acquired:
        return False

    try:
        redis = await _get_redis()
        try:
            result = await redis.eval(_RELEASE_LOCK_SCRIPT, 1, lock.key, lock.value)
            released = result == 1

            if released:
                log.info("task_lock.released", key=lock.key)
            else:
                log.warning("task_lock.release_failed", key=lock.key)

            return released
        finally:
            await redis.close()
    except Exception as exc:
        log.warning("task_lock.release_error", key=lock.key, error=str(exc)[:200])
        return False


class TaskLockContext:
    """任务锁上下文管理器 — 自动获取/释放锁。

    使用示例::

        async with TaskLockContext("process_document", doc_id) as lock:
            if not lock.acquired:
                return  # 已有其他 worker 在处理
            await do_work()

    降级策略：Redis 不可用时 acquired=True（放行），单机场景安全。
    """

    def __init__(
        self,
        task_name: str,
        task_id: str,
        ttl: int | None = None,
    ) -> None:
        self.task_name = task_name
        self.task_id = task_id
        self.ttl = ttl
        self._lock: TaskLock | None = None

    @property
    def acquired(self) -> bool:
        """是否成功获取锁。"""
        return self._lock.acquired if self._lock else False

    @property
    def key(self) -> str:
        """锁的 Redis key。"""
        return self._lock.key if self._lock else ""

    async def __aenter__(self) -> TaskLockContext:
        self._lock = await acquire_task_lock(self.task_name, self.task_id, self.ttl)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._lock:
            await release_task_lock(self._lock)
