"""
中间件配置 — 单一职责：注册 CORS、请求日志、API 限流中间件。

遵循单一职责：本模块仅负责中间件的注册与配置，
不包含业务逻辑（CORS 策略来自 Settings，日志格式由 structlog 处理）。
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.utils.logger import get_logger

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


# 全局限流器实例 — 在 setup_middleware 中初始化
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter | None:
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
        _rate_limiter = RateLimiter(
            per_minute=settings.RATE_LIMIT_PER_MINUTE,
            burst=settings.RATE_LIMIT_BURST,
        )
        log.info(
            "ratelimit.enabled",
            per_minute=settings.RATE_LIMIT_PER_MINUTE,
            burst=settings.RATE_LIMIT_BURST,
        )

    # --- 请求日志 + 限流中间件 ---
    @app.middleware("http")
    async def log_and_rate_limit(request: Request, call_next):
        """记录每个 HTTP 请求 + 按客户端限流。"""
        # 限流检查（健康检查等路径豁免）
        if _rate_limiter is not None:
            path = request.url.path
            if not path.startswith(_EXEMPT_PATHS):
                client_id = _get_client_id(request)
                if not _rate_limiter.allow(client_id):
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

        # 请求日志
        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
