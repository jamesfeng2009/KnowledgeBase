"""
通知实时推送 Hub — 基于 Redis Pub/Sub + SSE 实现站内通知实时推送。

架构：
    Celery 任务 / API 调用
        ↓ NotificationService._create_notification()
        ↓ NotificationHub.publish(user_id, payload)
        ↓ Redis PUBLISH notify:{user_id} <json>
        ↓
    FastAPI SSE 端点 GET /notifications/stream
        ↓ Redis SUBSCRIBE notify:{user_id}
        ↓ yield SSE event
        ↓
    浏览器 EventSource onmessage

设计要点：
    - Redis 不可用时静默降级（通知仍写入 DB，只是不实时推送）
    - SSE 端点每 30s 发送心跳保活，防止代理超时断连
    - 用户多标签页时每个标签页独立 SSE 连接，Redis Pub/Sub 天然 fan-out
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Redis channel 前缀
CHANNEL_PREFIX: str = "notify"

#: SSE 心跳间隔（秒）
HEARTBEAT_INTERVAL: int = 30

#: Redis 连接（延迟初始化）
_redis: Any = None


async def _get_redis() -> Any:
    """延迟获取 Redis 连接 — 避免模块加载时连接。"""
    global _redis
    if _redis is not None:
        return _redis
    try:
        import redis.asyncio as aioredis

        from app.config import get_settings

        settings = get_settings()
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        logger.info("notification_hub.redis_connected", url=settings.REDIS_URL)
    except Exception as exc:
        logger.warning("notification_hub.redis_unavailable", error=str(exc))
        _redis = None
    return _redis


def _channel_name(user_id: str | uuid.UUID) -> str:
    """构造用户专属通知频道名。"""
    return f"{CHANNEL_PREFIX}:{user_id}"


async def publish(user_id: str | uuid.UUID, payload: dict[str, Any]) -> bool:
    """发布通知到用户频道。

    Args:
        user_id: 目标用户 ID。
        payload: 通知内容（将 JSON 序列化后发布）。

    Returns:
        True 发布成功，False Redis 不可用或发布失败。
    """
    redis = await _get_redis()
    if redis is None:
        return False

    channel = _channel_name(user_id)
    try:
        message = json.dumps(payload, ensure_ascii=False, default=str)
        await redis.publish(channel, message)
        logger.info("notification_hub.published", channel=channel)
        return True
    except Exception as exc:
        logger.warning("notification_hub.publish_error", error=str(exc))
        return False


async def subscribe_stream(
    user_id: str | uuid.UUID,
) -> Any:
    """订阅用户频道并生成 SSE 事件流。

    生成器产出 SSE 格式文本块，直接供 StreamingResponse 消费。
    每 HEARTBEAT_INTERVAL 秒发送一次心跳注释保活。

    Args:
        user_id: 订阅用户 ID。

    Yields:
        str: SSE 格式文本块。
    """
    redis = await _get_redis()
    if redis is None:
        # Redis 不可用 — 发送降级提示后结束
        yield _format_sse({"type": "error", "message": "实时推送不可用"})
        yield _format_sse({"type": "done"}, event="done")
        return

    channel = _channel_name(user_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    logger.info("notification_hub.subscribed", channel=channel, user_id=str(user_id))

    try:
        # 心跳基于空闲时间戳：get_message 内部 1 秒超时即返回 None，
        # 外层 wait_for(HEARTBEAT_INTERVAL) 永远等不到 TimeoutError，
        # 导致心跳分支不可达、空闲连接被反代 idle 超时静默断开。
        # 改为记录上次产出时间，空闲超过 HEARTBEAT_INTERVAL 即补心跳。
        last_yield = time.monotonic()
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )

            if message is None:
                # 无消息 — 空闲超时则发送心跳保活，否则短暂让出控制权
                if time.monotonic() - last_yield >= HEARTBEAT_INTERVAL:
                    yield ": heartbeat\n\n"
                    last_yield = time.monotonic()
                else:
                    await asyncio.sleep(0.1)
                continue

            if message.get("type") == "message":
                data = message.get("data", "")
                # data 已是 JSON 字符串，直接转发
                yield f"data: {data}\n\n"
                last_yield = time.monotonic()
    except asyncio.CancelledError:
        logger.info("notification_hub.cancelled", channel=channel)
    except Exception as exc:
        logger.error("notification_hub.stream_error", error=str(exc))
        yield _format_sse({"type": "error", "message": str(exc)})
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass
        logger.info("notification_hub.unsubscribed", channel=channel)


def _format_sse(data: dict[str, Any], event: str | None = None) -> str:
    """格式化为 SSE 文本块。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"
