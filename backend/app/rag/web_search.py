"""P4 公网搜索提供商 — Deep Research 双路取证的 web 一路（Tavily 真实实现）。

只取理念、不动技术栈：轻量协议 + 真实 Tavily HTTP 调用；snippet 直用，不抓网页正文。

限速与配额（对齐项目"外部服务成本可控 / 并发受限"偏好）：
    - 并发上限：进程内 asyncio.Semaphore（WEB_SEARCH_CONCURRENCY，默认 2）全局共享
    - 令牌桶限速：WEB_SEARCH_RATE_PER_SECOND / WEB_SEARCH_RATE_BURST（内存实现）
    - 每日配额：WEB_SEARCH_DAILY_QUOTA；有 Redis 时跨实例计数，Redis 不可用降级为进程内计数
降级契约：
    - 无 API Key / provider 未配置 → MockProvider（返回空，不抛错）
    - HTTP 失败/限流/超时/解析失败 → 返回空列表，由上层 asyncio.gather(return_exceptions) 兜底
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

_TAVILY_ENDPOINT = "https://api.tavily.com/search"


class WebHit(dict):
    """统一公网命中结构（TypedDict 的运行时容器）。"""

    title: str
    url: str
    snippet: str
    score: float


class WebSearchProvider(Protocol):
    """公网搜索提供商协议。

    tenant_id 用于配额按租户隔离；None 时按全局 scope 计数。
    """

    async def search(
        self, query: str, max_results: int = 5, tenant_id: str | None = None
    ) -> list[dict]: ...


class MockProvider:
    """显式降级实现：始终返回空结果，保证"空库/无 Key 不阻塞"。"""

    async def search(
        self, query: str, max_results: int = 5, tenant_id: str | None = None
    ) -> list[dict]:
        return []


def dedup_web_by_url(hits: list[dict]) -> list[dict]:
    """仅 web 源内按 URL 去重（绝不跨源合并去重）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        url = h.get("url") or ""
        if url in seen:
            continue
        seen.add(url)
        out.append(h)
    return out


# ----------------------------------------------------------------------
# 限速 / 配额
# ----------------------------------------------------------------------

class TokenBucket:
    """内存令牌桶（快速路径），asyncio 友好。

    unlike 信号量，令牌以 rate 持续回填，burst 控制瞬发上限。
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate = max(rate_per_second, 0.0)
        self._capacity = max(burst, 1)
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._rate <= 0:
            return  # 不限速
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(
                    self._capacity, self._tokens + elapsed * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep(
                    (1.0 - self._tokens) / self._rate if self._rate > 0 else 0.1
                )


class DailyQuota:
    """每日配额计数：优先 Redis（跨实例），不可用降级为进程内计数。

    降级语义对齐 task_lock：Redis 异常不影响主流程（放行/本地计数）。
    """

    def __init__(
        self, daily_limit: int, per_tenant: bool = True, use_redis: bool = True
    ) -> None:
        self._limit = max(daily_limit, 0)
        self._per_tenant = per_tenant
        self._use_redis = use_redis
        self._local: dict[tuple[str, str], int] = defaultdict(int)

    @staticmethod
    def _date() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    async def consume(self, scope: str = "") -> bool:
        """尝试消费一次配额；返回 True=放行，False=已达当日配额上限。"""
        if self._limit <= 0:
            return True
        date = self._date()
        key = (scope, date)

        # Redis 路径（生产默认启用；不可用降级本地）
        if self._use_redis:
            redis = await self._try_redis_consume(key, date)
            if redis is not None:
                return redis

        # 内存降级路径
        self._local[key] += 1
        return self._local[key] <= self._limit

    async def _try_redis_consume(self, key: tuple[str, str], date: str) -> bool | None:
        """成功后返回是否放行；Redis 不可用返回 None（调用方走内存降级）。"""
        try:
            import redis.asyncio as aioredis

            settings = get_settings()
            r = await aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                rk = f"web_search_quota:{key[0] or 'global'}:{date}"
                val = await r.incr(rk)
                if val == 1:  # 首次写入，设 TTL 到当日结束
                    await r.expire(rk, self._ttl_redis())
                return val <= self._limit
            finally:
                await r.aclose()
        except Exception as exc:
            log.warning("web_search.quota_redis_unavailable", error=str(exc)[:200])
            return None

    @staticmethod
    def _ttl_redis() -> int:
        """距离当日 UTC 结束的秒数（约 TTL）。"""
        now = datetime.now(timezone.utc)
        end_of_day = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                      ).replace(day=now.day + 1)
        return max(int((end_of_day - now).total_seconds()), 3600)


# ----------------------------------------------------------------------
# Tavily 真实实现
# ----------------------------------------------------------------------

class TavilyProvider:
    """Tavily 搜索实现。snippet 直用返回的 content 字段。

    Args:
        api_key: Tavily API Key。
        client: 可选注入的 httpx.AsyncClient（测试用 MockTransport；默认走项目重试客户端）。
        search_depth: basic / advanced。
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        search_depth: str = "basic",
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required for TavilyProvider")
        self._api_key = api_key
        self._search_depth = search_depth
        self._client = client  # 惰性建默认客户端
        self._sem = _concurrency_semaphore()
        self._bucket = _rate_limiter()
        self._quota = _daily_quota()

    async def search(
        self, query: str, max_results: int = 5, tenant_id: str | None = None
    ) -> list[dict]:
        scope = f"tenant:{tenant_id}" if tenant_id else "global"
        if not await self._quota.consume(scope):
            log.warning("web_search.quota_exceeded", tenant_id=tenant_id)
            return []
        async with self._sem:
            await self._bucket.acquire()
            return await self._do_search(query, max_results)

    async def _do_search(self, query: str, max_results: int) -> list[dict]:
        client = self._client or _shared_http_client()
        try:
            resp = await client.post(
                _TAVILY_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "max_results": max_results,
                    "search_depth": self._search_depth,
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("web_search.tavily_failed", error=str(exc)[:200])
            return []
        try:
            data = resp.json()
        except Exception as exc:
            log.warning("web_search.tavily_parse_failed", error=str(exc)[:200])
            return []
        hits: list[dict] = []
        for item in data.get("results", []):
            if not item.get("url"):
                continue
            hits.append({
                "title": item.get("title", ""),
                "url": item["url"],
                "snippet": item.get("content") or "",
                "score": float(item.get("score", 0.0) or 0.0),
            })
        return hits


# ----------------------------------------------------------------------
# 进程内共享的限速 / 配额单例与默认 HTTP 客户端
# ----------------------------------------------------------------------

_sem: asyncio.Semaphore | None = None
_bucket: TokenBucket | None = None
_quota: DailyQuota | None = None
_client: httpx.AsyncClient | None = None


def _concurrency_semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(get_settings().WEB_SEARCH_CONCURRENCY)
    return _sem


def _rate_limiter() -> TokenBucket:
    global _bucket
    if _bucket is None:
        s = get_settings()
        _bucket = TokenBucket(s.WEB_SEARCH_RATE_PER_SECOND, s.WEB_SEARCH_RATE_BURST)
    return _bucket


def _daily_quota() -> DailyQuota:
    global _quota
    if _quota is None:
        s = get_settings()
        _quota = DailyQuota(s.WEB_SEARCH_DAILY_QUOTA, s.WEB_SEARCH_QUOTA_PER_TENANT)
    return _quota


def _shared_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        from app.utils.retry import build_retry_http_client

        _client = build_retry_http_client(timeout=30.0)
    return _client


def build_provider(name: str, api_key: str = "") -> object:
    """按名称构造提供商；Tavily 缺 Key / 未知名一律回落 MockProvider（降级不阻塞）。"""
    lower = (name or "mock").strip().lower()
    if lower == "tavily" and api_key:
        log.info("web_search.provider_built", provider="tavily")
        return TavilyProvider(api_key)
    if lower in {"tavily", "bing"}:
        log.warning("web_search.provider_missing_key", provider=lower)
    log.info("web_search.provider_fallback_mock", provider=lower or "mock")
    return MockProvider()