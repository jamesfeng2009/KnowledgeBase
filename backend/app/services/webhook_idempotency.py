"""Webhook 事件幂等去重 — 基于 Redis SETNX。

外部平台（飞书/Confluence）在 webhook 返回非 2xx 或超时会重试推送，
导致同一事件被多次投递。本模块用 Redis SETNX 记录已处理事件的 event_id，
命中即跳过，避免重复同步。

遵循单一职责：仅做幂等判定，不涉及事件解析或同步逻辑。
遵循优雅降级：Redis 不可用时返回 False（放行），单机场景可接受。
"""
from __future__ import annotations

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# 已处理事件保留时长（秒）— 1 小时覆盖外部平台最大重试窗口
_DEFAULT_TTL_SECONDS: int = 3600

# Redis key 前缀
_KEY_PREFIX: str = "ekb:webhook:event:"


async def is_duplicate_event(
    event_id: str, ttl: int = _DEFAULT_TTL_SECONDS
) -> bool:
    """检查事件是否已处理过（幂等去重）。

    用 SETNX 原子化标记：首次调用返回 False（未重复）并写入标记；
    后续相同 event_id 调用返回 True（已处理过）。

    Args:
        event_id: 事件唯一标识（飞书 header.event_id / Confluence eventId）。
        ttl: 标记保留时长（秒），默认 1 小时。

    Returns:
        True = 重复事件（已处理过，应跳过）；
        False = 首次事件（应处理）。
    """
    if not event_id:
        # 无 event_id 无法去重 — 放行（首次处理）
        return False

    try:
        import redis.asyncio as aioredis

        settings = get_settings()
        redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            key = f"{_KEY_PREFIX}{event_id}"
            # SET key 1 NX EX ttl — 仅当 key 不存在时设置
            acquired = await redis.set(key, "1", nx=True, ex=ttl)
            if acquired:
                log.info("webhook.event_marked", event_id=event_id, ttl=ttl)
                return False  # 首次，未重复
            log.info("webhook.event_duplicate", event_id=event_id)
            return True  # 已存在，重复
        finally:
            await redis.close()
    except Exception as exc:
        # Redis 不可用时放行（降级为无幂等，单机场景可接受）
        log.warning(
            "webhook.idempotency_redis_unavailable",
            event_id=event_id,
            error=str(exc)[:200],
        )
        return False
