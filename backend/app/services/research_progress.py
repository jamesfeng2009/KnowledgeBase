"""
Deep Research 进度事件源 — 基于 Redis Pub/Sub + 快照回放实现 SSE 实时流。

架构（对齐 notification_hub）:
    Celery worker（_deep_research_async）
        ↓ research() 的 progress 回调
        ↓ publish_progress(task_id, event)
        ↓ Redis RPUSH research_events:{task_id}（快照，限长）
        ↓ Redis PUBLISH research_progress:{task_id}
        ↓
    FastAPI 用户 GET /api/v1/research/{task_id}/stream
        ↓ subscribe_stream(task_id)：先回放快照再订阅实时事件 + 心跳
        ↓ 浏览器 EventSource onmessage

设计要点：
    - 快照回放：先订阅再 LRANGE 回放历史事件，保证"关标签页重开 / 断线重连"
      时进度完整恢复；快照末尾若已是 done，则回放完后直接收尾，不再空等。
    - 优雅降级：Redis 不可用不抛错（worker 端 publish 返回 False 不阻塞研究）；
      SSE 端返回降级提示后收尾。
    - 心跳保活：空闲超 HEARTBEAT_INTERVAL 补 SSE 注释心跳，防反代断开。
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncGenerator

from app.utils.logger import get_logger
from app.utils.sse import SSEEvent, SSEEventType

logger = get_logger(__name__)

#: Redis channel 前缀（实时推送）
CHANNEL_PREFIX: str = "research_progress"
#: Redis 快照 key 前缀（回放缓冲）
SNAPSHOT_PREFIX: str = "research_events"
#: 快照保留最大条数（LTRIM 裁剪）
MAX_SNAPSHOT: int = 200
#: SSE 心跳间隔（秒）
HEARTBEAT_INTERVAL: int = 30

#: 进度事件类型（event 字段，前端据此分派渲染）
EVENT_DECOMPOSED = "decomposed"      # data: {"type","topics":[...]}
EVENT_SUBTOPIC = "subtopic"          # data: {"type","index","total","subtopic","status"}
EVENT_OVERVIEW = "overview"          # data: {"type","summary","distributions"}
EVENT_DONE = SSEEventType.DONE       # data: {"type":"done","task_id"}


# Redis 连接（延迟初始化）
_redis: Any = None


async def _get_redis() -> Any:
    """延迟获取 Redis 连接 - 避免模块加载时连接，失败返回 None（降级）。"""
    global _redis
    if _redis is not None:
        return _redis
    try:
        import redis.asyncio as aioredis

        from app.config import get_settings

        settings = get_settings()
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        logger.info("research_progress.redis_connected", url=settings.REDIS_URL)
    except Exception as exc:
        logger.warning("research_progress.redis_unavailable", error=str(exc))
        _redis = None
    return _redis


def _channel(task_id: str) -> str:
    return f"{CHANNEL_PREFIX}:{task_id}"


def _snapshot_key(task_id: str) -> str:
    return f"{SNAPSHOT_PREFIX}:{task_id}"


# ----------------------------------------------------------------------
# Worker 端发布
# ----------------------------------------------------------------------

async def publish_progress(task_id: str, event: dict[str, Any]) -> bool:
    """将研究进度事件写入快照缓冲并发布到频道。

    Redis 不可用时返回 False 并记录告警，绝不影响研究流程本身。
    """
    redis = await _get_redis()
    if redis is None:
        return False
    try:
        payload = json.dumps(event, ensure_ascii=False)
        await redis.rpush(_snapshot_key(task_id), payload)
        await redis.ltrim(_snapshot_key(task_id), -MAX_SNAPSHOT, -1)
        await redis.publish(_channel(task_id), payload)
        return True
    except Exception as exc:
        logger.warning("research_progress.publish_failed", error=str(exc)[:200])
        return False


# ----------------------------------------------------------------------
# Web 端 SSE 订阅（含快照回放 + 心跳）
# ----------------------------------------------------------------------

async def subscribe_stream(task_id: str) -> AsyncGenerator[str, None]:
    """订阅任务进度并生成 SSE 文本流（供 StreamingResponse 消费）。

    顺序：先订阅频道（避免快照与实时之间丢事件）→ 回放快照事件 →
    若快照已含 done 则收尾返回；否则进入实时订阅循环（空闲心跳保活）。
    """
    redis = await _get_redis()
    if redis is None:
        # Redis 不可用 — 发送降级提示后由 sse_response 包装器补终态 done
        yield SSEEvent(
            {"type": "error", "message": "实时进度推送不可用"}, event="error"
        ).to_text()
        return

    pubsub = redis.pubsub()
    await pubsub.subscribe(_channel(task_id))
    logger.info("research_progress.subscribed", task_id=task_id)

    try:
        # 1) 回放快照
        snapshot = await redis.lrange(_snapshot_key(task_id), 0, -1)
        saw_done = False
        for raw in snapshot:
            item = _decode(raw)
            if item is None:
                continue
            if item.get("type") == EVENT_DONE:
                saw_done = True
            yield SSEEvent(item, event=item.get("type")).to_text()

        if saw_done:
            # 任务在连接前已结束 — 快照即完整历史，直接收尾
            return

        # 2) 实时订阅（空闲心跳保活）
        last_yield = time.monotonic()
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            now = time.monotonic()
            if message is None:
                if now - last_yield >= HEARTBEAT_INTERVAL:
                    yield ": heartbeat\n\n"
                    last_yield = now
                continue
            data = message.get("data")
            if not data:
                continue
            item = _decode(data)
            if item is None:
                continue
            yield SSEEvent(item, event=item.get("type")).to_text()
            last_yield = now
            if item.get("type") == EVENT_DONE:
                return
    finally:
        try:
            await pubsub.unsubscribe(_channel(task_id))
        except Exception:
            pass


def _decode(raw: Any) -> dict[str, Any] | None:
    """解析订阅/快照消息为事件 dict；非法 JSON 返回 None。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        item = json.loads(raw)
        return item if isinstance(item, dict) else None
    except (ValueError, TypeError):
        return None