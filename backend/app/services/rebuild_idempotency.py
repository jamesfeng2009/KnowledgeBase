"""推荐模型重建互斥锁 — 单一职责：同一租户同时只允许一个重建任务。

基于 Redis SETNX（与 webhook_idempotency 同风格）：
    - 任务派发前占位，值 = 预生成的 Celery task_id，重复提交直接回读原 id；
    - 派发失败显式释放；任务运行期由 TTL 兜底释放（重建为幂等覆盖写，
      重复执行结果一致，TTL 内拒绝重复只是省算力，不影响正确性）；
    - Redis 不可用时调用方优雅降级为无锁提交（放行）。
"""

from __future__ import annotations

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 重建进行中标记 TTL（秒）— 覆盖常规重建时长 + 管理员重试窗口
REBUILD_LOCK_TTL_SECONDS: int = 600

_KEY_PREFIX: str = "ekb:recommend:rebuild:inflight:"


def rebuild_lock_key(scope: str) -> str:
    """互斥锁 Redis key — scope 为租户 ID 字符串或 'default'。"""
    return f"{_KEY_PREFIX}{scope}"


async def acquire_rebuild_lock(
    scope: str, task_id: str, ttl: int = REBUILD_LOCK_TTL_SECONDS
) -> tuple[bool, str | None]:
    """尝试占用重建锁。

    Returns:
        (True, None)       — 抢到锁，调用方以 task_id 派发任务；
        (False, existing)  — 已有重建任务在跑，existing 为其 task_id。
    """
    import redis.asyncio as aioredis

    redis = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        acquired = await redis.set(
            rebuild_lock_key(scope), task_id, nx=True, ex=ttl
        )
        if acquired:
            return True, None
        existing = await redis.get(rebuild_lock_key(scope))
        return False, existing
    finally:
        await redis.close()


async def release_rebuild_lock(scope: str) -> None:
    """释放重建锁（派发失败时调用；Redis 不可用静默忽略）。"""
    try:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(
            get_settings().REDIS_URL, decode_responses=True
        )
        try:
            await redis.delete(rebuild_lock_key(scope))
        finally:
            await redis.close()
    except Exception as exc:
        log.warning(
            "recommend.rebuild.lock_release_failed", scope=scope, error=str(exc)[:200]
        )
