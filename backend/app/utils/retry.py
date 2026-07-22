"""
指数退避重试工具 — 三层重试体系共享入口。

三层职责划分（每层独立可配置，互不干扰）::

    L1  HTTP Transport  — httpx-retry AsyncRetryTransport，重试 5xx / 连接错误
    L2  函数级 tenacity  — @with_retry 装饰器，重试 DB / 文件 IO / 业务逻辑
    L3  Celery 任务级   — make_celery_retry_kwargs()，生成 retry_backoff 参数

遵循单一职责：本模块仅提供重试策略编排，不包含业务逻辑。
遵循依赖倒置：所有阈值从 app.config.get_settings() 获取，不硬编码。

幂等性约定：
    重试隐含"同一操作可能执行多次"。调用方必须确保被重试的操作是幂等的
    （如 DB upsert、Redis SETNX、HTTP GET/PUT）。非幂等操作（如 POST 创建）
    应在业务层使用幂等键或预检查，不能依赖本模块的重试。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

import httpx
from httpx_retry import AsyncRetryTransport, RetryPolicy
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ------------------------------------------------------------------
# L2: 函数级 tenacity 重试
# ------------------------------------------------------------------

def _log_retry(retry_state: RetryCallState) -> None:
    """tenacity before_sleep 回调 — 记录每次重试日志。"""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    log.warning(
        "retry.attempt_failed",
        fn=retry_state.fn.__name__ if retry_state.fn else "unknown",
        attempt=retry_state.attempt_number,
        next_action="retry" if not retry_state.outcome.failed else "give_up",
        error=str(exc)[:200] if exc else "unknown",
    )


def with_retry(
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    jitter: float | None = None,
    retry_exceptions: tuple[type[Exception], ...] = (
        ConnectionError,
        TimeoutError,
        OSError,
        httpx.HTTPStatusError,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
    ),
) -> Callable[[F], F]:
    """tenacity 指数退避重试装饰器（L2）。

    所有参数可选，缺省时从 Settings 读取全局配置，确保环境变量覆盖生效。

    Args:
        max_attempts: 最大尝试次数（含首次），默认 settings.RETRY_MAX_ATTEMPTS。
        base_delay: 基础延迟秒数（第一次重试等待 ≈ base_delay），默认 settings.RETRY_BACKOFF_BASE_DB。
        max_delay: 延迟上限秒数，默认 settings.RETRY_BACKOFF_MAX。
        jitter: 抖动范围秒数（全抖动模式），默认 settings.RETRY_JITTER。
        retry_exceptions: 触发重试的异常类型，默认网络/连接类异常。

    Returns:
        装饰后的函数，失败时按指数退避重试。

    使用示例::

        @with_retry()
        async def fetch_embedding(text: str) -> list[float]:
            ...

        @with_retry(max_attempts=5, base_delay=2.0)
        async def upload_to_r2(data: bytes) -> None:
            ...
    """
    settings = get_settings()
    _max_attempts = max_attempts or settings.RETRY_MAX_ATTEMPTS
    _base_delay = base_delay if base_delay is not None else settings.RETRY_BACKOFF_BASE_DB
    _max_delay = max_delay if max_delay is not None else settings.RETRY_BACKOFF_MAX
    _jitter = jitter if jitter is not None else settings.RETRY_JITTER

    def decorator(fn: F) -> F:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            @retry(
                stop=stop_after_attempt(_max_attempts),
                wait=wait_exponential_jitter(
                    initial=_base_delay,
                    max=_max_delay,
                    jitter=_jitter,
                ),
                retry=retry_if_exception_type(retry_exceptions),
                before_sleep=_log_retry,
                reraise=True,
            )
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(fn)
            @retry(
                stop=stop_after_attempt(_max_attempts),
                wait=wait_exponential_jitter(
                    initial=_base_delay,
                    max=_max_delay,
                    jitter=_jitter,
                ),
                retry=retry_if_exception_type(retry_exceptions),
                before_sleep=_log_retry,
                reraise=True,
            )
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return fn(*args, **kwargs)

            return wrapper  # type: ignore[return-value]

    return decorator


# ------------------------------------------------------------------
# L1: HTTP Transport 重试
# ------------------------------------------------------------------

def build_retry_http_client(
    *,
    timeout: float = 60.0,
    max_retries: int | None = None,
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    retry_status_codes: list[int] | None = None,
) -> httpx.AsyncClient:
    """创建带指数退避重试的 httpx.AsyncClient（L1）。

    使用 httpx-retry 的 AsyncRetryTransport 作为传输层，自动重试
    5xx 服务端错误和 429 限流。连接级错误（ConnectError、ReadTimeout）
    也由 transport 自动重试。

    幂等性：GET / PUT / DELETE 自动安全重试；POST 默认不重试（由 retry_on
    状态码控制），业务层需自行确保 POST 幂等或使用幂等键。

    Args:
        timeout: 请求超时秒数。
        max_retries: 最大重试次数，默认 settings.RETRY_MAX_ATTEMPTS。
        base_url: 基础 URL（可选）。
        headers: 默认请求头。
        retry_status_codes: 触发重试的 HTTP 状态码列表，
            默认 [429, 500, 502, 503, 504]。

    Returns:
        配置好重试 transport 的 httpx.AsyncClient。

    使用示例::

        client = build_retry_http_client(timeout=30.0, base_url="http://milvus:19530")
        resp = await client.post("/v2/vectors/search", json=payload)
    """
    settings = get_settings()
    _max_retries = max_retries if max_retries is not None else settings.RETRY_MAX_ATTEMPTS
    _retry_codes = retry_status_codes or [429, 500, 502, 503, 504]

    policy = RetryPolicy(
        max_retries=_max_retries,
        initial_delay=settings.RETRY_BACKOFF_BASE,
        max_delay=settings.RETRY_BACKOFF_MAX,
        multiplier=2.0,
        retry_on=_retry_codes,
    )

    transport = AsyncRetryTransport(policy=policy)

    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "transport": transport,
    }
    if base_url:
        client_kwargs["base_url"] = base_url
    if headers:
        client_kwargs["headers"] = headers

    return httpx.AsyncClient(**client_kwargs)


# ------------------------------------------------------------------
# L3: Celery 任务级重试
# ------------------------------------------------------------------

def make_celery_retry_kwargs() -> dict[str, Any]:
    """生成 Celery @task 装饰器的指数退避重试参数（L3）。

    从 Settings 读取配置，返回可直接展开到 @celery_app.task() 的字典。
    Celery 的 retry_backoff 使用以下公式计算延迟::

        delay = base * (2 ** (attempt - 1))

    其中 base = RETRY_BACKOFF_BASE_CELERY，上限为 RETRY_BACKOFF_MAX。

    Returns:
        包含 retry_backoff / retry_backoff_max / retry_jitter / max_retries
        的字典。

    使用示例::

        from app.utils.retry import make_celery_retry_kwargs

        @celery_app.task(
            name="tasks.document_tasks.process_document",
            bind=True,
            **make_celery_retry_kwargs(),
        )
        def process_document(self, doc_id: str) -> dict:
            ...
    """
    settings = get_settings()
    return {
        "max_retries": settings.RETRY_MAX_ATTEMPTS,
        "retry_backoff": settings.RETRY_BACKOFF_BASE_CELERY,
        "retry_backoff_max": int(settings.RETRY_BACKOFF_MAX),
        "retry_jitter": True,
        "autoretry_for": (
            ConnectionError,
            TimeoutError,
            OSError,
        ),
    }
