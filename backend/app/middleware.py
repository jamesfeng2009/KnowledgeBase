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

import time
import uuid
from uuid import UUID

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.utils.crypto import decode_access_token
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
    """

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

    async def _ensure_redis(self):
        """惰性初始化 Redis 连接，失败则标记降级模式。"""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            # 预加载 Lua 脚本（EVALSHA 比 EVAL 快，省去每次脚本传输）
            self._lua_sha = await self._redis.script_load(_RATE_LIMIT_LUA)
            self._degraded = False
            return self._redis
        except Exception as exc:
            log.warning("ratelimit.redis_init_failed", error=str(exc)[:200])
            self._degraded = True
            return None

    async def allow(self, client_id: str) -> bool:
        """检查客户端是否被允许通过（异步，Redis 原子化）。"""
        redis_conn = await self._ensure_redis()
        if redis_conn is None or self._degraded:
            # 降级模式：用内存限流器
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
            # Redis 运行时故障 → 临时降级为内存模式
            log.warning("ratelimit.redis_error_fallback", error=str(exc)[:200])
            self._degraded = True
            return self._fallback.allow(client_id)

    def clear(self) -> None:
        """清空内存降级桶（测试用，不影响 Redis 中的状态）。"""
        self._fallback.clear()


# 全局限流器实例 — 在 setup_middleware 中初始化
_rate_limiter: RateLimiter | RedisRateLimiter | None = None


def get_rate_limiter() -> RateLimiter | RedisRateLimiter | None:
    """获取全局限流器实例（测试可访问）。"""
    return _rate_limiter


def _get_client_id(request: Request) -> str:
    """提取客户端标识 — 优先 API Key，回退到 IP。"""
    api_key = request.headers.get("x-api-key") or request.headers.get("authorization")
    if api_key:
        return f"key:{api_key[:32]}"  # 截断防止过长 key 撑爆内存

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
    global _rate_limiter

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
    if settings.RATE_LIMIT_ENABLED:
        # P0 分布式预备：优先使用 Redis-backed 限流器，多实例共享计数
        # Redis 不可用时自动降级为内存令牌桶
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url:
            _rate_limiter = RedisRateLimiter(
                per_minute=settings.RATE_LIMIT_PER_MINUTE,
                burst=settings.RATE_LIMIT_BURST,
                redis_url=redis_url,
            )
            log.info(
                "ratelimit.enabled_redis",
                per_minute=settings.RATE_LIMIT_PER_MINUTE,
                burst=settings.RATE_LIMIT_BURST,
            )
        else:
            _rate_limiter = RateLimiter(
                per_minute=settings.RATE_LIMIT_PER_MINUTE,
                burst=settings.RATE_LIMIT_BURST,
            )
            log.info(
                "ratelimit.enabled_memory",
                per_minute=settings.RATE_LIMIT_PER_MINUTE,
                burst=settings.RATE_LIMIT_BURST,
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
        tenant_id: UUID | None = None
        path = request.url.path
        if not path.startswith(_EXEMPT_PATHS):
            # 从 Authorization header 解析 JWT 提取 tenant_id
            auth_header = request.headers.get("authorization", "")
            token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""
            if token:
                try:
                    payload = decode_access_token(token)
                    tid_str = payload.get("tenant_id")
                    if tid_str:
                        tenant_id = UUID(tid_str)
                except Exception:
                    pass  # 无效 JWT 不阻断请求，后续 get_current_user 会处理 401

        request.state.tenant_id = tenant_id

        # 将 tenant_id 和 request_id 绑定到 structlog 上下文变量，
        # 使当前请求内所有日志条目自动携带 tenant_id 和 request_id
        structlog.contextvars.bind_contextvars(
            tenant_id=str(tenant_id) if tenant_id else None,
            request_id=request_id,
        )

        try:
            # --- 限流检查（健康检查等路径豁免） ---
            if _rate_limiter is not None:
                if not path.startswith(_EXEMPT_PATHS):
                    client_id = _get_client_id(request)
                    # RedisRateLimiter.allow 是异步的，RateLimiter.allow 是同步的
                    if isinstance(_rate_limiter, RedisRateLimiter):
                        allowed = await _rate_limiter.allow(client_id)
                    else:
                        allowed = _rate_limiter.allow(client_id)
                    if not allowed:
                        log.warning(
                            "ratelimit.exceeded",
                            client_id=client_id,
                            path=path,
                        )
                        return JSONResponse(
                            status_code=429,
                            content={
                                "code": 429,
                                "message": "请求过于频繁，请稍后再试",
                            },
                            headers={
                                "Retry-After": "60",
                            },
                        )

            # --- 请求日志 ---
            start_time = time.perf_counter()
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

            # P0-Stage3: 响应头回传 request_id，供客户端/前端关联追踪
            response.headers["X-Request-ID"] = request_id

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
