"""
LLM Provider 专用重试装饰器 —— 支持 async generator 的指数退避重试。

设计动机：
    LLM Provider 的 chat 方法是 async generator（流式 yield 文本片段），
    tenacity 的 @retry 不能直接装饰 async generator（会把 generator 当 coroutine）。
    需要专门的装饰器处理流式场景的重试边界。

核心策略（与 prodagent http_retry.py 对齐）::

    ┌─ attempt 1 ─────────────────────────────────┐
    │  try: 消费第一个 chunk                       │
    │  ├─ 成功 → 继续消费剩余 chunks，完成返回     │
    │  └─ 失败 → classify_error 判断是否重试       │
    └─────────────────────────────────────────────┘
                    │
                    ▼ (retryable & attempts < max)
    ┌─ attempt 2 ─────────────────────────────────┐
    │  重新调用 chat() 创建新 generator            │
    │  ...                                        │
    └─────────────────────────────────────────────┘

关键设计：
    1. 流式开始前（第一个 chunk 之前）的错误 → 按 classify_error 判断是否重试
       这是连接 / 认证 / 限流 / overloaded 最容易发生的阶段。
    2. 流式开始后（已 yield 过 chunk）的错误 → 直接抛，不重试
       原因：重试会导致已 yield 的文本片段重复，SSE 场景用户会看到重复内容。
    3. asyncio.CancelledError 永不被吞 —— 直接 raise，不重试
       生产级关键细节：吞 CancelledError 会导致超时后任务无法真正终止。
    4. Retry-After header 优先于指数退避
       服务端说等多久就等多久，但受 max_delay 封顶，防止异常 header 导致长等待。
    5. Full Jitter 退避 —— uniform(0, exponential)，避免惊群效应
    6. 不可重试错误（AUTH_INVALID / QUOTA_EXHAUSTED / CONTENT_BLOCKED 等）立即抛

与现有熔断器的关系：
    chat 方法体内已有熔断器逻辑（检查 state + 记录 success/failure）。
    with_llm_retry 装饰在 chat 外层 —— 重试时 chat 会重新检查熔断器 state。
    若熔断器 OPEN，chat 抛 CircuitBreakerOpenError，被 classify_error 识别为
    UNKNOWN（不可重试），所以不会重试熔断中的请求。

使用示例::

    from app.utils.llm_retry import with_llm_retry

    class AnthropicProvider(LLMProvider):
        @with_llm_retry(provider="anthropic")
        async def chat(self, messages, tools=None, stream=False, **kwargs):
            # 熔断器检查 + API 调用 + yield 文本
            ...

遵循单一职责：本模块仅提供 LLM 重试编排，不含业务逻辑。
遵循依赖倒置：所有阈值从 app.config.get_settings() 获取。
"""

from __future__ import annotations

import asyncio
import functools
import random
from typing import Any, AsyncIterator, Callable, TypeVar

from app.config import get_settings
from app.core.error_classifier import classify_error
from app.core.error_reason import ErrorLayer
from app.utils.logger import get_logger

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., AsyncIterator[Any]])


def with_llm_retry(
    provider: str,
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
) -> Callable[[F], F]:
    """LLM Provider 专用重试装饰器 —— 支持 async generator。

    Args:
        provider: LLM provider 名（anthropic / openai / dashscope / vllm），
                  用于 classify_error 和日志归类。
        max_attempts: 最大尝试次数（含首次），默认 settings.LLM_RETRY_MAX_ATTEMPTS。
        base_delay: 基础延迟秒数，默认 settings.LLM_RETRY_BACKOFF_BASE。
        max_delay: 延迟上限秒数，默认 settings.LLM_RETRY_BACKOFF_MAX。

    Returns:
        装饰后的 async generator function，失败时按指数退避重试。

    重试策略：
        - 仅在"第一个 chunk 之前"的错误重试
        - CancelledError 永不被吞（直接 raise）
        - Retry-After header 优先，否则 Full Jitter 指数退避
        - 不可重试错误（AUTH_INVALID / QUOTA_EXHAUSTED 等）立即抛
    """
    settings = get_settings()
    _max_attempts = max_attempts if max_attempts is not None else settings.LLM_RETRY_MAX_ATTEMPTS
    _base_delay = base_delay if base_delay is not None else settings.LLM_RETRY_BACKOFF_BASE
    _max_delay = max_delay if max_delay is not None else settings.LLM_RETRY_BACKOFF_MAX

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            attempt = 0
            # 优先用 self._circuit_breaker_name 作为 provider 名 ——
            # 让 DashScopeProvider 等子类自动用正确的 provider 标识
            # （DashScope 继承 VLLMProvider.chat，但 _circuit_breaker_name="dashscope"）
            actual_provider = getattr(self, "_circuit_breaker_name", None) or provider
            while True:
                attempt += 1
                # 标记是否已 yield 过 chunk —— 已 yield 后的错误不重试
                first_chunk_yielded = False
                # 每次重试创建新 generator（重新发起 API 请求）
                gen = fn(self, *args, **kwargs)
                try:
                    # 尝试消费第一个 chunk —— 这是最容易失败的阶段
                    # （连接 / 认证 / 限流 / overloaded 通常在此抛出）
                    try:
                        first_chunk = await gen.__anext__()
                    except StopAsyncIteration:
                        # 空 generator，正常结束
                        return

                    # 第一个 chunk 成功 —— 后续错误不重试（避免重复 yield）
                    first_chunk_yielded = True
                    yield first_chunk
                    async for chunk in gen:
                        yield chunk
                    # 成功完成
                    return

                except asyncio.CancelledError:
                    # 永不被吞！直接 raise，不重试
                    # 生产级关键细节：吞 CancelledError 会导致超时后任务无法真正终止
                    raise
                except Exception as exc:
                    # 流式开始后的错误：直接抛，不重试
                    # 重试会导致已 yield 的文本片段重复，SSE 场景用户会看到重复内容
                    if first_chunk_yielded:
                        raise

                    # 流式开始前的错误：分类并判断是否重试
                    model = getattr(self, "default_model", None)
                    classified = classify_error(
                        exc,
                        layer=ErrorLayer.LLM,
                        provider=actual_provider,
                        model=model,
                    )

                    # 不可重试 or 已达最大尝试次数：立即抛
                    if not classified.retryable or attempt >= _max_attempts:
                        log.warning(
                            "llm.retry.give_up",
                            attempt=attempt,
                            max_attempts=_max_attempts,
                            **classified.to_dict(),  # 含 provider / model / reason / retryable 等
                        )
                        raise

                    # 计算延迟：Retry-After 优先，否则 Full Jitter 指数退避
                    if classified.retry_after_seconds is not None:
                        delay = classified.retry_after_seconds
                    else:
                        # Full Jitter: uniform(0, exponential)
                        # AWS 推荐策略，避免惊群效应
                        exponential = min(
                            _base_delay * (2.0 ** (attempt - 1)),
                            _max_delay,
                        )
                        delay = random.uniform(0.0, exponential)

                    log.warning(
                        "llm.retry.attempt",
                        attempt=attempt,
                        max_attempts=_max_attempts,
                        delay_seconds=round(delay, 2),
                        **classified.to_dict(),  # 含 provider / model / reason / retryable 等
                    )
                    await asyncio.sleep(delay)
                    # while 循环回到顶部，attempt += 1，重新调用 fn 创建新 generator

        return wrapper  # type: ignore[return-value]

    return decorator
