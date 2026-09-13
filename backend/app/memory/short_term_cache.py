"""L1 短期窗口 Redis 热层 — 单一职责：短期对话消息的高速缓存（对标竞品滑动窗口）。

定位（对标竞品「Redis 短期记忆滑动窗口」）：
- Redis List 保存每个会话最近 N 条消息（RPUSH + LTRIM），TTL 24h；
- 读取优先走 Redis（O(1)），miss 时由调用方回填 PostgreSQL 数据；
- Redis 不可用时所有方法静默降级（调用方自然回退 PG 直查路径）。

写穿透时机（ChatService）：
- 用户消息落库后 append(user)；
- 助手消息落库后 append(assistant)；
- 删除会话时 invalidate()。

遵循单一职责：只管缓存的读写与失效，不管消息持久化（那是 MessageRepository 的事）。
"""

from __future__ import annotations

import json
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "mem:l1"


class ShortTermCache:
    """短期窗口 Redis 热层 — Redis List 实现，优雅降级。"""

    def __init__(
        self,
        redis_url: str | None = None,
        ttl_hours: int | None = None,
        window: int | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._redis: Any = None
        from app.config import get_settings

        settings = get_settings()
        self._enabled = settings.MEMORY_SHORT_TERM_CACHE_ENABLED
        self._ttl_hours = ttl_hours or settings.MEMORY_SHORT_TERM_CACHE_TTL_HOURS
        self._window = window or settings.MEMORY_SHORT_TERM_CACHE_WINDOW

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _key(self, conversation_id: str) -> str:
        return f"{_KEY_PREFIX}:{conversation_id}"

    def _get_redis(self) -> Any | None:
        """惰性获取 Redis 客户端 — 不可用时返回 None（降级直查 PG）。"""
        if not self._enabled:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis

                url = self._redis_url
                if not url:
                    from app.config import get_settings

                    url = get_settings().REDIS_URL
                self._redis = aioredis.from_url(url, decode_responses=True)
            except Exception as exc:
                logger.warning("short_term_cache.redis_init_failed", error=str(exc))
                self._enabled = False
                return None
        return self._redis

    @staticmethod
    def _decode(raw: list[str]) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        for item in raw:
            try:
                d = json.loads(item)
                if isinstance(d, dict) and d.get("role") and "content" in d:
                    msgs.append({"role": d["role"], "content": d["content"]})
            except Exception:
                continue
        return msgs

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def append(
        self, conversation_id: str, role: str, content: str
    ) -> bool:
        """写穿透一条消息 — RPUSH + LTRIM 保留最近 window 条 + 续期 TTL。"""
        redis = self._get_redis()
        if redis is None:
            return False
        try:
            key = self._key(conversation_id)
            payload = json.dumps(
                {"role": role, "content": content}, ensure_ascii=False
            )
            async with redis.pipeline(transaction=True) as pipe:
                pipe.rpush(key, payload)
                pipe.ltrim(key, -self._window, -1)
                pipe.expire(key, self._ttl_hours * 3600)
                await pipe.execute()
            return True
        except Exception as exc:
            logger.warning("short_term_cache.append_failed", error=str(exc))
            return False

    async def get_window(
        self, conversation_id: str, limit: int | None = None
    ) -> list[dict[str, str]] | None:
        """读取最近 limit 条消息（时间正序）；缓存 miss 返回 None。

        Args:
            conversation_id: 会话 ID。
            limit: 返回条数上限，None 时返回热层内全部（≤ window）。
        """
        redis = self._get_redis()
        if redis is None:
            return None
        try:
            key = self._key(conversation_id)
            n = limit or self._window
            raw = await redis.lrange(key, -n, -1)
            if not raw:
                return None  # miss：调用方回填 PG
            return self._decode(raw)
        except Exception as exc:
            logger.warning("short_term_cache.get_failed", error=str(exc))
            return None

    async def replace(
        self, conversation_id: str, messages: list[dict[str, str]]
    ) -> bool:
        """用 PG 数据重建热层（TTL 过期 / miss 回填场景）。"""
        redis = self._get_redis()
        if redis is None:
            return False
        try:
            key = self._key(conversation_id)
            tail = messages[-self._window:]
            if not tail:
                return True
            payloads = [
                json.dumps({"role": m["role"], "content": m["content"]}, ensure_ascii=False)
                for m in tail
            ]
            async with redis.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.rpush(key, *payloads)
                pipe.expire(key, self._ttl_hours * 3600)
                await pipe.execute()
            return True
        except Exception as exc:
            logger.warning("short_term_cache.replace_failed", error=str(exc))
            return False

    async def invalidate(self, conversation_id: str) -> bool:
        """删除会话时清理热层。"""
        redis = self._get_redis()
        if redis is None:
            return False
        try:
            await redis.delete(self._key(conversation_id))
            return True
        except Exception as exc:
            logger.warning("short_term_cache.invalidate_failed", error=str(exc))
            return False


# 模块级单例 — 全局共享一个 Redis 连接
short_term_cache = ShortTermCache()
