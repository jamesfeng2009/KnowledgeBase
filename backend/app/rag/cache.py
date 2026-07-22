"""
Token 缓存 — 单一职责：缓存 RAG 生成的答案，降低重复查询的 LLM 调用成本。

三级缓存，从快到慢：
    L1 — Redis 精确缓存：key = sha256(tenant_id + query)，TTL 1h。
    L2 — 简化语义缓存：用 embedding 相似度 > 0.95 匹配，TTL 24h。
         （进程内有界 LRU + embedding 对比实现，避免引入 GPTCache 等重依赖；
          容量由 CACHE_L2_MAX_SIZE 控制，默认 1000，超容逐出最久未使用条目）
    L3 — 模型原生 Prompt Caching：session 级，由 LLM Provider 在请求层处理，
         本模块不介入，仅在未命中 L1/L2 时返回 None 让上层走 Provider。

多租户隔离（安全）：
    缓存 key 由 tenant_id 与 query 联合计算，L2 条目同样按 tenant_id 隔离，
    不同租户的相同/相似 query 互不可见，杜绝跨租户答案泄漏。

遵循单一职责：本模块只负责缓存读写，不涉及检索与生成逻辑。
遵循依赖倒置：Redis 地址、Embedder 均通过依赖注入获取，可替换为 Mock。
遵循优雅降级：Redis 不可用时回退到 L2 内存缓存，不抛异常。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.llm.embedder import EmbeddingProvider, get_embedder
from app.utils.logger import get_logger

if TYPE_CHECKING:
    import redis.asyncio as aioredis

log = get_logger(__name__)

# 缓存 TTL（秒）
_L1_TTL: int = 3600  # 1h
_L2_TTL: int = 86400  # 24h
# L2 语义匹配相似度阈值
_L2_SIMILARITY_THRESHOLD: float = 0.95

settings = get_settings()


@dataclass
class _L2Entry:
    """L2 语义缓存条目 — 存储 query 文本、答案、embedding、租户与过期时间。"""

    query: str
    answer: str
    embedding: list[float]
    expire_at: float
    tenant_id: str | None = None


class TokenCache:
    """三级 Token 缓存 — L1(Redis) → L2(内存语义) → L3(Provider 原生)。

    使用方式::

        cache = TokenCache()
        if cached := await cache.get(query):
            return cached
        answer = await generate(...)
        await cache.set(query, answer)
    """

    def __init__(
        self,
        redis: aioredis.Redis | None = None,
        embedder: EmbeddingProvider | None = None,
        l2_max_size: int | None = None,
    ) -> None:
        self._redis: aioredis.Redis | None = redis
        self._embedder: EmbeddingProvider | None = embedder
        # L2 内存缓存：query_hash → _L2Entry
        # 有界 LRU（OrderedDict）：容量可配置（默认 CACHE_L2_MAX_SIZE=1000），
        # 超容逐出最久未使用条目；TTL 语义不变（过期条目读时跳过、写时清理）。
        self._l2_max_size: int = max(1, l2_max_size if l2_max_size is not None else settings.CACHE_L2_MAX_SIZE)
        self._l2_store: OrderedDict[str, _L2Entry] = OrderedDict()
        self._redis_available: bool | None = None

    # ------------------------------------------------------------------
    # 懒初始化（避免在导入期建立连接）
    # ------------------------------------------------------------------

    async def _get_redis(self) -> aioredis.Redis | None:
        """懒初始化 Redis 连接 — 首次调用时建立，失败则标记不可用并返回 None。"""
        if self._redis_available is False:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis_runtime

            self._redis = aioredis_runtime.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
            await self._redis.ping()
            self._redis_available = True
            log.info("cache.redis.connected", url=settings.REDIS_URL)
        except Exception as exc:
            self._redis_available = False
            self._redis = None
            log.warning("cache.redis.unavailable", error=str(exc))
        return self._redis

    async def _get_embedder(self) -> EmbeddingProvider | None:
        """懒初始化 Embedder — 失败则返回 None（L2 语义缓存降级禁用）。"""
        if self._embedder is not None:
            return self._embedder
        try:
            self._embedder = get_embedder()
        except Exception as exc:
            log.warning("cache.embedder.unavailable", error=str(exc))
        return self._embedder

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def get(self, query: str, tenant_id: str | None = None) -> str | None:
        """查询缓存 — 依次走 L1 精确 → L2 语义，命中即返回，否则 None。

        L3（模型原生 Prompt Caching）由 LLM Provider 在请求层处理，
        此处不介入；返回 None 表示缓存未命中，上层应走完整 RAG 流程。

        Args:
            query: 用户查询文本。
            tenant_id: 租户 ID（安全隔离）— 不同租户的缓存互不可见。
        """
        # L1: Redis 精确缓存
        cached = await self._l1_get(query, tenant_id)
        if cached is not None:
            log.debug("cache.hit", level="L1", query_hash=self._hash(query, tenant_id)[:12])
            return cached

        # L2: 内存语义缓存
        cached = await self._l2_get(query, tenant_id)
        if cached is not None:
            log.debug("cache.hit", level="L2", query_hash=self._hash(query, tenant_id)[:12])
            # 回填 L1 加速后续精确命中
            await self._l1_set(query, cached, ttl=_L1_TTL, tenant_id=tenant_id)
            return cached

        # L3: 由 Provider 处理，本层不命中
        return None

    async def set(self, query: str, answer: str, tenant_id: str | None = None) -> None:
        """写入缓存 — 同时写入 L1（精确）与 L2（语义）。

        Args:
            query: 用户查询文本。
            answer: 生成的答案。
            tenant_id: 租户 ID（安全隔离）— 与 get 传入值一致才可命中。
        """
        await self._l1_set(query, answer, ttl=_L1_TTL, tenant_id=tenant_id)
        await self._l2_set(query, answer, tenant_id)

    # ------------------------------------------------------------------
    # L1: Redis 精确缓存
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(query: str, tenant_id: str | None = None) -> str:
        """计算缓存 key 的 sha256 摘要 — 租户 ID 参与计算，隔离跨租户缓存。"""
        return hashlib.sha256(f"{tenant_id or ''}\n{query}".encode("utf-8")).hexdigest()

    async def _l1_get(self, query: str, tenant_id: str | None = None) -> str | None:
        """从 Redis 读取精确缓存。"""
        redis = await self._get_redis()
        if redis is None:
            return None
        try:
            key = f"cache:l1:{self._hash(query, tenant_id)}"
            value: Any = await redis.get(key)
            return value if isinstance(value, str) else None
        except Exception as exc:
            log.warning("cache.l1.get_error", error=str(exc))
            return None

    async def _l1_set(
        self,
        query: str,
        answer: str,
        ttl: int,
        tenant_id: str | None = None,
    ) -> None:
        """写入 Redis 精确缓存，失败仅记录日志。"""
        redis = await self._get_redis()
        if redis is None:
            return
        try:
            key = f"cache:l1:{self._hash(query, tenant_id)}"
            await redis.set(key, answer, ex=ttl)
        except Exception as exc:
            log.warning("cache.l1.set_error", error=str(exc))

    # ------------------------------------------------------------------
    # L2: 内存语义缓存
    # ------------------------------------------------------------------

    async def _l2_get(self, query: str, tenant_id: str | None = None) -> str | None:
        """从内存语义缓存查询 — embedding 余弦相似度 > 阈值即命中。

        仅匹配同一 tenant_id 的条目，避免跨租户语义命中导致的答案泄漏。
        """
        embedder = await self._get_embedder()
        if embedder is None:
            return None
        try:
            query_vec = (await embedder.embed([query]))[0]
        except Exception as exc:
            log.warning("cache.l2.embed_error", error=str(exc))
            return None

        now = time.time()
        best_score: float = 0.0
        best_key: str | None = None
        best_answer: str | None = None
        for key, entry in self._l2_store.items():
            if entry.expire_at < now:
                continue
            if entry.tenant_id != tenant_id:
                continue
            score = _cosine_similarity(query_vec, entry.embedding)
            if score > best_score:
                best_score = score
                best_key = key
                best_answer = entry.answer
        if best_score >= _L2_SIMILARITY_THRESHOLD:
            if best_key is not None:
                # LRU：命中条目标记为最近使用
                self._l2_store.move_to_end(best_key)
            return best_answer
        return None

    async def _l2_set(self, query: str, answer: str, tenant_id: str | None = None) -> None:
        """写入内存语义缓存 — 存储 query 的 embedding 与所属租户供后续相似度匹配。"""
        embedder = await self._get_embedder()
        if embedder is None:
            return
        try:
            query_vec = (await embedder.embed([query]))[0]
        except Exception as exc:
            log.warning("cache.l2.embed_error", error=str(exc))
            return
        self._purge_expired()
        key = self._hash(query, tenant_id)
        self._l2_store[key] = _L2Entry(
            query=query,
            answer=answer,
            embedding=query_vec,
            expire_at=time.time() + _L2_TTL,
            tenant_id=tenant_id,
        )
        self._l2_store.move_to_end(key)
        # 有界 LRU：超容逐出最久未使用条目（队首）
        while len(self._l2_store) > self._l2_max_size:
            self._l2_store.popitem(last=False)

    def _purge_expired(self) -> None:
        """清理过期的 L2 条目，避免内存无限增长。"""
        now = time.time()
        expired = [k for k, v in self._l2_store.items() if v.expire_at < now]
        for k in expired:
            self._l2_store.pop(k, None)

    async def close(self) -> None:
        """关闭 Redis 连接 — 应用关闭时调用。"""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:
                log.warning("cache.redis.close_error", error=str(exc))
            finally:
                self._redis = None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
