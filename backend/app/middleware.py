"""
中间件配置 — 单一职责：注册 CORS、请求日志、API 限流、租户上下文中间件。

遵循单一职责：本模块仅负责中间件的注册与配置，
不包含业务逻辑（CORS 策略来自 Settings，日志格式由 structlog 处理）。

分布式预备（P0）：限流器支持 Redis-backed 模式，多 API 实例共享计数。
Redis 不可用时自动降级为内存令牌桶，保证限流功能始终可用。

多租户隔离（P0）：TenantContextMiddleware 从 JWT 解析 tenant_id，
写入 request.state.tenant_id，供下游依赖注入和 Repository 使用。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from uuid import UUID

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.utils.logger import get_logger
from app.utils.request_context import set_request_id, reset_request_id

log = get_logger(__name__)


class TokenBucket:
    """令牌桶限流器 — 单客户端维度。

    桶容量 = burst（突发上限），每秒补充 rate = per_minute / 60 个令牌。
    线程安全由 GIL 保证（FastAPI 单进程 asyncio）。
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self._capacity = capacity
        self._refill_rate = refill_per_second
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()

    def try_consume(self, tokens: int = 1) -> bool:
        """尝试消费令牌，成功返回 True。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)

        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    def time_until_available(self, tokens: int = 1) -> float:
        """估算补足指定令牌数所需的等待秒数（0 = 立即可用）。

        只读估算，不消费令牌、不更新内部状态。
        """
        now = time.monotonic()
        projected = min(
            self._capacity,
            self._tokens + (now - self._last_refill) * self._refill_rate,
        )
        deficit = tokens - projected
        if deficit <= 0:
            return 0.0
        return deficit / self._refill_rate


class RateLimiter:
    """按客户端维度的限流管理器 — 内存令牌桶。

    客户端标识优先使用 X-API-Key 请求头，回退到客户端 IP。
    桶实例按客户端隔离，惰性创建。

    分布式场景下多 API 实例各自计数，限流精度为 N × 单实例配额。
    生产多实例环境应使用 RedisRateLimiter 替代。
    """

    def __init__(self, per_minute: int, burst: int) -> None:
        self._per_minute = per_minute
        self._burst = burst
        self._buckets: dict[str, TokenBucket] = {}

    def _get_bucket(self, client_id: str) -> TokenBucket:
        bucket = self._buckets.get(client_id)
        if bucket is None:
            bucket = TokenBucket(
                capacity=self._burst,
                refill_per_second=self._per_minute / 60.0,
            )
            self._buckets[client_id] = bucket
        return bucket

    def allow(self, client_id: str) -> bool:
        """检查客户端是否被允许通过。"""
        return self._get_bucket(client_id).try_consume()

    def estimate_wait(self, client_id: str) -> float:
        """估算该客户端获得下一个令牌的等待秒数（P2-12 排队用）。"""
        return self._get_bucket(client_id).time_until_available()

    def clear(self) -> None:
        """清空所有桶（测试用）。"""
        self._buckets.clear()


# ------------------------------------------------------------------
# Redis-backed 限流器 — 分布式多实例共享计数（P0 预备工作）
# ------------------------------------------------------------------

# Lua 脚本：原子化令牌桶取令牌
# KEYS[1] = rate_limit:{client_id}
# ARGV[1] = capacity（桶容量）
# ARGV[2] = refill_per_second（每秒补充速率）
# ARGV[3] = tokens_to_consume（消费令牌数，通常 1）
# ARGV[4] = now（当前时间戳，秒）
# ARGV[5] = ttl（key 过期时间，秒，避免无限累积）
# 返回 1 = 允许，0 = 拒绝
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local consume = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = now - last_refill
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if tokens >= consume then
    tokens = tokens - consume
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
redis.call('EXPIRE', key, ttl)
return allowed
"""


class RedisRateLimiter:
    """Redis-backed 限流器 — 多 API 实例共享计数。

    分布式场景下所有 API 实例共享 Redis 中的令牌桶状态，
    限流精度为单实例配额（而非 N × 单实例）。

    降级策略：Redis 不可用时自动降级为内存 RateLimiter，
    保证限流功能始终可用（单机降级模式下限流精度降低但功能不丢）。
    降级可自愈：进入降级后经过冷却窗口（_DEGRADED_COOLDOWN_SECONDS）允许
    重试 Redis 路径，成功即退出降级；失败则刷新冷却起点继续内存降级。
    """

    #: 降级自愈冷却窗口（秒）— 抖动后间隔该时长才重试 Redis 路径，
    #: 避免故障期间每次请求都额外付出一次连接尝试的代价
    _DEGRADED_COOLDOWN_SECONDS: float = 30.0

    def __init__(
        self,
        per_minute: int,
        burst: int,
        redis_url: str,
        key_prefix: str = "rate_limit:",
        key_ttl: int = 120,
    ) -> None:
        self._per_minute = per_minute
        self._burst = burst
        self._refill_per_second = per_minute / 60.0
        self._key_prefix = key_prefix
        self._key_ttl = key_ttl
        self._redis_url = redis_url
        self._redis = None
        self._lua_sha = None
        # 降级用内存限流器（Redis 不可用时 fallback）
        self._fallback = RateLimiter(per_minute=per_minute, burst=burst)
        self._degraded = False
        # 进入降级的时间戳（monotonic），供冷却窗口判断；未降级为 None
        self._degraded_since: float | None = None

    def _mark_degraded(self) -> None:
        """标记进入降级模式并记录冷却起点（供自愈重试判断）。"""
        self._degraded = True
        self._degraded_since = time.monotonic()

    async def _ensure_redis(self):
        """惰性初始化 Redis 连接，失败则标记降级模式。"""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            # 预加载 Lua 脚本（EVALSHA 比 EVAL 快，省去每次脚本传输）
            self._lua_sha = await self._redis.script_load(_RATE_LIMIT_LUA)
            # 连接成功 — 若此前处于降级状态则自愈退出降级
            self._degraded = False
            self._degraded_since = None
            return self._redis
        except Exception as exc:
            log.warning("ratelimit.redis_init_failed", error=str(exc)[:200])
            self._mark_degraded()
            return None

    async def allow(self, client_id: str) -> bool:
        """检查客户端是否被允许通过（异步，Redis 原子化）。"""
        if self._degraded:
            # 冷却窗口内直接走内存 fallback — Redis 健康时的热路径不受任何影响
            since = self._degraded_since
            if since is not None and (
                time.monotonic() - since < self._DEGRADED_COOLDOWN_SECONDS
            ):
                return self._fallback.allow(client_id)
            # 冷却到期 — 旧连接可能已损坏，置空强制重建后重试 Redis 路径
            self._redis = None

        redis_conn = await self._ensure_redis()
        if redis_conn is None:
            return self._fallback.allow(client_id)

        try:
            key = f"{self._key_prefix}{client_id}"
            now = time.time()
            result = await redis_conn.evalsha(
                self._lua_sha,
                1,
                key,
                str(self._burst),
                str(self._refill_per_second),
                "1",
                str(now),
                str(self._key_ttl),
            )
            return bool(int(result))
        except Exception as exc:
            # Redis 运行时故障 → 降级为内存模式，冷却窗口后可自愈
            log.warning("ratelimit.redis_error_fallback", error=str(exc)[:200])
            self._mark_degraded()
            return self._fallback.allow(client_id)

    def estimate_wait(self, client_id: str) -> float:
        """估算等待秒数（P2-12 排队用）。

        分布式场景下精确值需额外 RTT 读取 Redis 桶状态，此处用
        "补足一个令牌所需时间"近似 —— 足够支撑排队/直拒的决策。
        """
        return 1.0 / self._refill_per_second

    def clear(self) -> None:
        """清空内存降级桶（测试用，不影响 Redis 中的状态）。"""
        self._fallback.clear()


# 全局限流器实例 — 在 setup_middleware 中初始化
_rate_limiter: RateLimiter | RedisRateLimiter | None = None

# P1: 租户维度限流器实例 — 按 tenant_id 隔离，与客户端级限流并存。
_tenant_rate_limiter: RateLimiter | RedisRateLimiter | None = None

# P2-12: 排队缓冲计数器 — 当前正在排队等待令牌的请求数。
# 仅作上限闸门防止排队堆积拖垮进程；自增/自减发生在 await 之间的
# 同步代码段，单事件循环内无竞态。
_queued_requests: int = 0


def get_queued_request_count() -> int:
    """获取当前排队中的请求数（测试/监控用）。"""
    return _queued_requests


async def _try_queued_consume(
    client_id: str,
    max_wait_ms: int,
    max_queued: int,
) -> tuple[bool, float]:
    """P2-12: 排队等待令牌 — 在预计等待时间内缓冲请求而非直接 429。

    Args:
        client_id: 客户端标识。
        max_wait_ms: 允许排队的最大预计等待毫秒数（超出直接拒绝）。
        max_queued: 全局同时排队请求上限（超出直接拒绝）。

    Returns:
        (是否最终获得令牌, 实际排队等待毫秒数)
    """
    global _queued_requests

    if _rate_limiter is None:
        return False, 0.0

    estimated_s = _rate_limiter.estimate_wait(client_id)
    if estimated_s * 1000 > max_wait_ms:
        return False, 0.0
    if _queued_requests >= max_queued:
        return False, 0.0

    _queued_requests += 1
    t0 = time.monotonic()
    try:
        # 等待估算时长后重试消费；令牌桶按时间补充，到期大概率可消费
        await asyncio.sleep(estimated_s)
        if isinstance(_rate_limiter, RedisRateLimiter):
            allowed = await _rate_limiter.allow(client_id)
        else:
            allowed = _rate_limiter.allow(client_id)
        waited_ms = round((time.monotonic() - t0) * 1000, 2)
        if allowed:
            log.info(
                "ratelimit.queue_admitted",
                client_id=client_id,
                waited_ms=waited_ms,
            )
        return allowed, waited_ms
    finally:
        _queued_requests -= 1


def get_rate_limiter() -> RateLimiter | RedisRateLimiter | None:
    """获取全局限流器实例（测试可访问）。"""
    return _rate_limiter


def get_tenant_rate_limiter() -> RateLimiter | RedisRateLimiter | None:
    """获取租户维度限流器实例（测试可访问）。"""
    return _tenant_rate_limiter


def _build_limiter(
    per_minute: int, burst: int, redis_url: str | None
) -> RateLimiter | RedisRateLimiter:
    """按配置构建限流器 — Redis 可用时用分布式，否则内存令牌桶。"""
    if redis_url:
        return RedisRateLimiter(
            per_minute=per_minute,
            burst=burst,
            redis_url=redis_url,
        )
    return RateLimiter(per_minute=per_minute, burst=burst)


def _get_client_id(request: Request) -> str:
    """提取客户端标识 — 优先 API Key / 用户令牌，回退到 IP。

    注意：不能直接取 authorization 头前缀 —— JWT 的前 32 字符是固定的
    "Bearer eyJhbGciOiJIUzI1NiIs..."（header base64 相同），会导致所有
    登录用户共享同一个限流桶（一人超限全员 429）。对完整凭证取哈希，
    既区分不同用户/密钥，又避免明文凭证进入内存索引与日志。
    """
    api_key = request.headers.get("x-api-key") or request.headers.get("authorization")
    if api_key:
        digest = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        return f"key:{digest}"

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"

    return f"ip:{request.client.host if request.client else 'unknown'}"


# 不限流的路径前缀
_EXEMPT_PATHS: tuple[str, ...] = ("/health", "/docs", "/openapi.json", "/redoc")


def setup_middleware(app: FastAPI) -> None:
    """注册所有中间件到 FastAPI 应用实例。

    注册顺序（从外到内）：
    1. CORS — 处理跨域预检请求；
    2. 请求日志 — 记录每个请求的方法、路径、状态码与耗时；
    3. API 限流 — 按客户端令牌桶限流，超限返回 429。

    Args:
        app: FastAPI 应用实例。
    """
    global _rate_limiter, _tenant_rate_limiter

    settings = get_settings()

    # --- CORS ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- 初始化限流器 ---
    redis_url = getattr(settings, "REDIS_URL", None)
    if settings.RATE_LIMIT_ENABLED:
        _rate_limiter = _build_limiter(
            per_minute=settings.RATE_LIMIT_PER_MINUTE,
            burst=settings.RATE_LIMIT_BURST,
            redis_url=redis_url,
        )
        log.info(
            "ratelimit.enabled_client",
            per_minute=settings.RATE_LIMIT_PER_MINUTE,
            burst=settings.RATE_LIMIT_BURST,
        )

    # P1: 租户维度限流 — 独立于客户端级限流，按 tenant_id 隔离。
    if settings.RATE_LIMIT_TENANT_ENABLED:
        _tenant_rate_limiter = _build_limiter(
            per_minute=settings.RATE_LIMIT_TENANT_PER_MINUTE,
            burst=settings.RATE_LIMIT_TENANT_BURST,
            redis_url=redis_url,
        )
        log.info(
            "ratelimit.enabled_tenant",
            per_minute=settings.RATE_LIMIT_TENANT_PER_MINUTE,
            burst=settings.RATE_LIMIT_TENANT_BURST,
        )

    # --- 请求日志 + 限流 + 租户上下文 + request_id 中间件 ---
    @app.middleware("http")
    async def log_rate_limit_tenant_context(request: Request, call_next):
        """记录每个 HTTP 请求 + 按客户端限流 + 注入租户上下文 + 生成 request_id。

        租户上下文通过 structlog.contextvars 绑定到当前请求的整个生命周期，
        确保所有日志条目（包括 Service 层日志）都自动携带 tenant_id。

        P0-Stage3: 生成 request_id 并绑定到 contextvars，使引擎、服务层、
        LangFuse 追踪和 UsageRecord 均可关联同一请求。
        """

        # --- P0-Stage3: 生成 request_id ---
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        _rid_token = set_request_id(request_id)

        # --- 租户上下文注入（在限流之前，确保所有请求都有租户上下文） ---
        path = request.url.path
        tenant_id: UUID | None = None
        tenant_domain: str | None = None
        if not path.startswith(_EXEMPT_PATHS):
            # 解析租户标识：JWT 优先 → 网关 X-Tenant-Id 头 → 子域。
            # 中间件保持无 DB 访问（子域权威映射由需要时经 TenantService 完成）。
            from app.core.tenant_resolver import resolve_tenant_id

            tenant_id, tenant_domain = resolve_tenant_id(request, settings)
        request.state.tenant_id = tenant_id
        request.state.tenant_domain = tenant_domain

        # 将 tenant_id 和 request_id 绑定到 structlog 上下文变量，
        # 使当前请求内所有日志条目自动携带 tenant_id 和 request_id
        structlog.contextvars.bind_contextvars(
            tenant_id=str(tenant_id) if tenant_id else None,
            request_id=request_id,
        )

        try:
            # --- 限流检查（健康检查等路径豁免） ---
            queued_wait_ms: float | None = None
            if _rate_limiter is not None:
                if not path.startswith(_EXEMPT_PATHS):
                    client_id = _get_client_id(request)
                    # RedisRateLimiter.allow 是异步的，RateLimiter.allow 是同步的
                    if isinstance(_rate_limiter, RedisRateLimiter):
                        allowed = await _rate_limiter.allow(client_id)
                    else:
                        allowed = _rate_limiter.allow(client_id)
                    if not allowed:
                        # P2-12: 429 前短队列缓冲 — 预计等待可接受时排队取令牌，
                        # 成功则放行（响应头带排队耗时），失败/超时再 429。
                        queue_enabled = getattr(
                            settings, "RATE_LIMIT_QUEUE_ENABLED", True
                        )
                        if queue_enabled:
                            admitted, queued_wait_ms = await _try_queued_consume(
                                client_id,
                                max_wait_ms=getattr(
                                    settings, "RATE_LIMIT_QUEUE_MAX_WAIT_MS", 2000
                                ),
                                max_queued=getattr(
                                    settings, "RATE_LIMIT_QUEUE_MAX_QUEUED", 20
                                ),
                            )
                            if admitted:
                                allowed = True
                    if not allowed:
                        # Retry-After 取令牌补充周期的整数秒，替代固定 60s
                        retry_after = max(
                            1,
                            math.ceil(_rate_limiter.estimate_wait(client_id)),
                        )
                        log.warning(
                            "ratelimit.exceeded",
                            client_id=client_id,
                            path=path,
                            retry_after=retry_after,
                        )
                        return JSONResponse(
                            status_code=429,
                            content={
                                "code": 429,
                                "message": "请求过于频繁，请稍后再试",
                                "retry_after_seconds": retry_after,
                            },
                            headers={
                                "Retry-After": str(retry_after),
                            },
                        )

            # --- P1 租户维度限流：按 tenant_id 隔离，防止单租户合计流量打爆共享资源 ---
            if (
                _tenant_rate_limiter is not None
                and tenant_id is not None
                and not path.startswith(_EXEMPT_PATHS)
            ):
                tid_str = str(tenant_id)
                if isinstance(_tenant_rate_limiter, RedisRateLimiter):
                    tenant_allowed = await _tenant_rate_limiter.allow(f"tenant:{tid_str}")
                else:
                    tenant_allowed = _tenant_rate_limiter.allow(f"tenant:{tid_str}")
                if not tenant_allowed:
                    t_retry_after = max(
                        1,
                        math.ceil(_tenant_rate_limiter.estimate_wait(f"tenant:{tid_str}")),
                    )
                    log.warning(
                        "ratelimit.tenant_exceeded",
                        tenant_id=tid_str,
                        path=path,
                        retry_after=t_retry_after,
                    )
                    if settings.METRICS_ENABLED:
                        from app.utils import metrics

                        metrics.record_tenant_ratelimit_denied(tid_str)
                    return JSONResponse(
                        status_code=429,
                        content={
                            "code": 429,
                            "message": "工作区请求过于频繁，请稍后再试",
                            "retry_after_seconds": t_retry_after,
                        },
                        headers={
                            "Retry-After": str(t_retry_after),
                        },
                    )

            # --- 请求日志 ---
            start_time = time.perf_counter()
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

            # P1: Prometheus 指标采集（HTTP 请求计数 + 耗时直方图）
            if settings.METRICS_ENABLED:
                from app.utils import metrics

                metrics.record_http_request(
                    request.method,
                    request.url.path,
                    response.status_code,
                    duration_ms / 1000.0,
                    str(tenant_id) if tenant_id else "",
                )

            # P0-Stage3: 响应头回传 request_id，供客户端/前端关联追踪
            response.headers["X-Request-ID"] = request_id
            # P2-12: 排队放行的请求带排队耗时，便于客户端/监控感知降级
            if queued_wait_ms is not None:
                response.headers["X-RateLimit-Queued-Ms"] = str(queued_wait_ms)

            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
            return response
        finally:
            # 清理 structlog 上下文变量，避免跨请求泄漏
            structlog.contextvars.clear_contextvars()
            # P0-Stage3: 恢复 request_id contextvar
            reset_request_id(_rid_token)
