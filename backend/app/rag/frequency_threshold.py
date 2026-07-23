"""
基于查询频率的动态匹配阈值调节器（P2 — 借鉴点 4：动态匹配阈值）。

核心思路
========
检索质量守卫原先使用 *静态* 阈值 ``RAG_RETRIEVAL_SCORE_THRESHOLD``（0.3）判断
重排分数均值是否合格。静态阈值在两类场景下效果欠佳：

- **高频（热门）查询**：知识库中有大量高质量匹配内容，低阈值会放行噪声文档，
  反而稀释答案质量；
- **低频（冷门）查询**：匹配内容稀缺，高阈值会漏召回，导致"无结果"或"答非所问"。

本模块按查询频次动态调节阈值：

    频次 >= RAG_THRESHOLD_FREQ_HOT_COUNT  → 阈值上浮 HOT_BOOST（更严格筛选）
    频次 <  RAG_THRESHOLD_FREQ_HOT_COUNT  → 阈值下浮 COLD_DROP（更宽松召回）
    最终值被 clamp 到 [RAG_THRESHOLD_MIN, RAG_THRESHOLD_MAX]

存储与降级
==========
- 优先 **Redis**（``INCR`` + ``EXPIRE`` 滑动窗口），跨实例共享频次统计；
- Redis 不可用时降级为**进程内 LRU 计数器**（仅当前实例可见，相对频次仍有效）；
- 总开关 ``RAG_DYNAMIC_THRESHOLD_ENABLED=False`` 时，``get_threshold`` 直接返回
  静态阈值，``record_query`` 成为空操作 — 零开销回归旧行为。

调用方式
========

    from app.rag.frequency_threshold import FrequencyBasedThreshold

    fbt = FrequencyBasedThreshold()
    await fbt.record_query("报销流程是什么")          # 记录一次查询
    threshold = await fbt.get_threshold("报销流程是什么")  # 获取动态阈值
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# 进程内 LRU 计数器最大容量（Redis 不可用时的降级存储）
_MEM_MAX_ENTRIES: int = 10_000
# Redis key 前缀
_REDIS_PREFIX: str = "ekb:qfreq:"
# 重试探测间隔（秒）— Redis 不可用后定期重试恢复
_RETRY_INTERVAL: float = 30.0

# 查询归一化：去除首尾空白 + 折叠连续空白 + 转小写
_WS_RE = re.compile(r"\s+")


def _normalize_query(query: str) -> str:
    """归一化查询文本 — 折叠空白 + 转小写，使大小写/空格差异不计入频次。"""
    if not query:
        return ""
    return _WS_RE.sub(" ", query.strip()).lower()


def _query_hash(query: str) -> str:
    """归一化查询的稳定哈希 — 用作 Redis key，避免长查询撑大 key 体积。"""
    return hashlib.sha256(_normalize_query(query).encode("utf-8")).hexdigest()[:16]


class FrequencyBasedThreshold:
    """基于查询频率的动态匹配阈值调节器。

    线程安全说明：
        - Redis 路径天然线程安全（原子 INCR）；
        - 进程内 LRU 路径在单事件循环 async 上下文下安全（无真并行）。
    """

    def __init__(self, redis: Any = None) -> None:
        """初始化调节器。

        Args:
            redis: 可选的已建立 Redis 连接（``redis.asyncio.Redis``）。
                   未传入时懒初始化；初始化失败则降级为进程内计数器。
        """
        import time

        self._redis: Any = redis
        self._redis_available: bool | None = None
        # Redis 不可用后的下次重试时间（monotonic）
        self._retry_at: float = 0.0
        self._time = time.monotonic
        # 进程内 LRU 计数器（降级存储）：{query_hash: count}
        self._mem_store: "OrderedDict[str, int]" = OrderedDict()

    # ------------------------------------------------------------------
    # 内部：Redis 懒初始化（与 TokenCache 保持一致的降级策略）
    # ------------------------------------------------------------------

    async def _get_redis(self) -> Any | None:
        """懒初始化 Redis 连接 — 失败则标记不可用并定期重试。"""
        if self._redis_available is False:
            # 降级期间定期探测，避免 Redis 恢复后仍走内存路径
            if self._time() < self._retry_at:
                return None
            log.debug("frequency_threshold.redis.retry_probe")

        if self._redis is not None:
            return self._redis

        settings = get_settings()
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
            await self._redis.ping()
            self._redis_available = True
            log.info("frequency_threshold.redis.connected", url=settings.REDIS_URL)
        except Exception as exc:
            self._redis_available = False
            self._redis = None
            self._retry_at = self._time() + _RETRY_INTERVAL
            log.warning("frequency_threshold.redis.unavailable", error=str(exc))
        return self._redis

    # ------------------------------------------------------------------
    # 配置访问（每次读取最新，支持运行时热更新与测试 mock）
    # ------------------------------------------------------------------

    @property
    def _settings(self) -> Any:
        return get_settings()

    @property
    def enabled(self) -> bool:
        """动态阈值总开关。"""
        return getattr(self._settings, "RAG_DYNAMIC_THRESHOLD_ENABLED", True)

    @property
    def _base_threshold(self) -> float:
        """静态基准阈值 — 动态调节的起点。"""
        return getattr(self._settings, "RAG_RETRIEVAL_SCORE_THRESHOLD", 0.3)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def record_query(self, query: str) -> int:
        """记录一次查询，返回归一化查询的当前频次。

        总开关关闭时为空操作（返回 0），零开销。

        Args:
            query: 用户原始查询文本。

        Returns:
            该查询（归一化后）的累计频次；总开关关闭或异常时返回 0。
        """
        if not self.enabled or not query or not query.strip():
            return 0

        qhash = _query_hash(query)
        settings = self._settings
        ttl: int = getattr(settings, "RAG_THRESHOLD_FREQ_TTL", 86400)

        redis = await self._get_redis()
        if redis is not None:
            try:
                key = f"{_REDIS_PREFIX}{qhash}"
                count = await redis.incr(key)
                # 仅在首次创建时设置 TTL（INCR 后 count==1），避免每次重置窗口
                if count == 1:
                    await redis.expire(key, ttl)
                return int(count)
            except Exception as exc:
                log.warning(
                    "frequency_threshold.redis.incr_error", error=str(exc)
                )
                # 标记不可用，后续走内存路径
                self._redis_available = False
                self._retry_at = self._time() + _RETRY_INTERVAL

        # 降级：进程内 LRU 计数器
        return self._mem_incr(qhash)

    async def get_threshold(self, query: str) -> float:
        """获取查询的动态匹配阈值。

        计算逻辑：
            base = RAG_RETRIEVAL_SCORE_THRESHOLD
            count = 该查询频次
            count >= HOT_COUNT → base + HOT_BOOST
            count <  HOT_COUNT → base - COLD_DROP
            clamp(result, MIN, MAX)

        总开关关闭时直接返回静态基准阈值。

        Args:
            query: 用户原始查询文本。

        Returns:
            动态阈值（float）。无频次数据时按低频处理。
        """
        base = self._base_threshold
        if not self.enabled or not query or not query.strip():
            return base

        count = await self._get_count(query)
        return self._compute_threshold(base, count)

    def _compute_threshold(self, base: float, count: int) -> float:
        """纯函数：根据频次计算阈值（无 IO，便于单元测试）。"""
        settings = self._settings
        hot_count: int = getattr(settings, "RAG_THRESHOLD_FREQ_HOT_COUNT", 10)
        hot_boost: float = getattr(settings, "RAG_THRESHOLD_HOT_BOOST", 0.1)
        cold_drop: float = getattr(settings, "RAG_THRESHOLD_COLD_DROP", 0.05)
        min_val: float = getattr(settings, "RAG_THRESHOLD_MIN", 0.1)
        max_val: float = getattr(settings, "RAG_THRESHOLD_MAX", 0.6)

        if count >= hot_count:
            threshold = base + hot_boost
        else:
            threshold = base - cold_drop

        # clamp 到合法区间，确保 cold 不低于地板、hot 不超过天花板
        threshold = max(min_val, min(max_val, threshold))

        log.debug(
            "frequency_threshold.computed",
            base=base,
            count=count,
            threshold=round(threshold, 4),
            tier="hot" if count >= hot_count else "cold",
        )
        return round(threshold, 4)

    async def _get_count(self, query: str) -> int:
        """读取查询频次（不递增）。"""
        qhash = _query_hash(query)
        redis = await self._get_redis()
        if redis is not None:
            try:
                key = f"{_REDIS_PREFIX}{qhash}"
                raw = await redis.get(key)
                return int(raw) if raw else 0
            except Exception as exc:
                log.warning(
                    "frequency_threshold.redis.get_error", error=str(exc)
                )
                self._redis_available = False
                self._retry_at = self._time() + _RETRY_INTERVAL

        # 降级：进程内计数器
        return self._mem_store.get(qhash, 0)

    # ------------------------------------------------------------------
    # 进程内 LRU 计数器（降级存储）
    # ------------------------------------------------------------------

    def _mem_incr(self, qhash: str) -> int:
        """进程内计数器自增（LRU 淘汰最旧条目）。"""
        count = self._mem_store.get(qhash, 0) + 1
        self._mem_store[qhash] = count
        self._mem_store.move_to_end(qhash)
        # 容量超限时淘汰最旧条目
        while len(self._mem_store) > _MEM_MAX_ENTRIES:
            self._mem_store.popitem(last=False)
        return count

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """关闭 Redis 连接（进程内计数器无需清理）。"""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
