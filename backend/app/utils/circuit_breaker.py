"""
熔断器（Circuit Breaker）— 防止级联故障的核心组件。

状态机（三态）::

    CLOSED ──(连续失败 ≥ threshold)──▶ OPEN
       ▲                                 │
       │                                 │ (recovery_timeout 到期)
       │                                 ▼
       └──(探测成功)── HALF_OPEN ◀──(自动降级)
                          │
                          └──(探测失败)──▶ OPEN

遵循单一职责：仅管理熔断状态机，不关心业务逻辑。
遵循依赖倒置：所有阈值从 app.config.get_settings() 获取。

使用方式（装饰器）::

    from app.utils.circuit_breaker import circuit_call

    @circuit_call("dashscope")
    async def call_llm(prompt: str) -> str:
        ...

使用方式（上下文管理器）::

    from app.utils.circuit_breaker import get_circuit_breaker

    cb = get_circuit_breaker("milvus")
    async with cb:
        result = await milvus_client.search(...)

状态查询（API 层）::

    from app.utils.circuit_breaker import get_all_circuit_status

    statuses = get_all_circuit_status()  # {"dashscope": "closed", "milvus": "open", ...}
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class CircuitState(str, Enum):
    """熔断器三态。"""

    CLOSED = "closed"      # 正常放行
    OPEN = "open"          # 熔断中，快速失败
    HALF_OPEN = "half_open"  # 半开，允许探测


class CircuitBreakerOpenError(Exception):
    """熔断器开启时抛出的异常。"""

    def __init__(self, name: str, state: CircuitState) -> None:
        self.name = name
        self.state = state
        super().__init__(
            f"熔断器 '{name}' 处于 {state.value} 状态，请求被快速失败。"
            f"请稍后重试或检查下游服务状态。"
        )


@dataclass
class CircuitBreaker:
    """单个熔断器实例 — 管理一个下游服务的熔断状态。

    线程安全：使用 threading.Lock 保护状态转换，兼容跨事件循环场景。

    Attributes:
        name: 熔断器名称（如 "dashscope", "milvus"）。
        failure_threshold: 连续失败次数触发熔断。
        recovery_timeout: OPEN → HALF_OPEN 冷却时间（秒）。
        half_open_max_calls: 半开状态最多探测请求数。
        state: 当前状态。
        failure_count: 连续失败计数。
        last_failure_time: 上次失败时间戳。
        half_open_calls: 半开状态已发出的探测请求数。
    """

    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """从 Settings 读取默认值（如果未显式指定）。"""
        settings = get_settings()
        if self.failure_threshold == 5:
            self.failure_threshold = settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD
        if self.recovery_timeout == 30.0:
            self.recovery_timeout = settings.CIRCUIT_BREAKER_RECOVERY_TIMEOUT
        if self.half_open_max_calls == 1:
            self.half_open_max_calls = settings.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS

    def _should_transition_to_half_open(self) -> bool:
        """检查是否应从 OPEN 转为 HALF_OPEN。"""
        if self.state != CircuitState.OPEN:
            return False
        elapsed = time.monotonic() - self.last_failure_time
        return elapsed >= self.recovery_timeout

    def _check_and_enter(self) -> None:
        """检查熔断状态，允许或拒绝调用（同步，加锁）。"""
        with self._lock:
            if self.state == CircuitState.OPEN:
                if self._should_transition_to_half_open():
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    log.info(
                        "circuit_breaker.transition",
                        name=self.name,
                        from_state="open",
                        to_state="half_open",
                    )
                else:
                    log.warning(
                        "circuit_breaker.rejected",
                        name=self.name,
                        state="open",
                        failure_count=self.failure_count,
                    )
                    raise CircuitBreakerOpenError(self.name, self.state)

            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    log.warning(
                        "circuit_breaker.rejected",
                        name=self.name,
                        state="half_open",
                        reason="max_probe_calls_reached",
                    )
                    raise CircuitBreakerOpenError(self.name, self.state)
                self.half_open_calls += 1

    async def __aenter__(self) -> CircuitBreaker:
        """异步上下文管理器入口 — 检查熔断状态。"""
        self._check_and_enter()
        return self

    async def __aexit__(
        self,
        exc_type: type[Exception] | None,
        exc_val: Exception | None,
        exc_tb: Any,
    ) -> None:
        """异步上下文管理器出口 — 记录成功/失败。"""
        if exc_type is not None:
            self._record_failure()
        else:
            self._record_success()

    def _record_success(self) -> None:
        """记录一次成功调用（同步，加锁）。"""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.half_open_calls = 0
                log.info(
                    "circuit_breaker.transition",
                    name=self.name,
                    from_state="half_open",
                    to_state="closed",
                    reason="probe_success",
                )
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0

    def _record_failure(self) -> None:
        """记录一次失败调用（同步，加锁）。"""
        with self._lock:
            self.last_failure_time = time.monotonic()

            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.failure_count = 0
                self.half_open_calls = 0
                log.warning(
                    "circuit_breaker.transition",
                    name=self.name,
                    from_state="half_open",
                    to_state="open",
                    reason="probe_failure",
                )
            elif self.state == CircuitState.CLOSED:
                self.failure_count += 1
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    log.warning(
                        "circuit_breaker.transition",
                        name=self.name,
                        from_state="closed",
                        to_state="open",
                        failure_count=self.failure_count,
                        threshold=self.failure_threshold,
                    )

    def get_status(self) -> dict[str, Any]:
        """获取当前状态快照。"""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
                "half_open_max_calls": self.half_open_max_calls,
                "last_failure_time": self.last_failure_time,
                "half_open_calls": self.half_open_calls,
            }


# ------------------------------------------------------------------
# 全局注册表 — 按名称管理熔断器实例
# ------------------------------------------------------------------

_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    failure_threshold: int | None = None,
    recovery_timeout: float | None = None,
    half_open_max_calls: int | None = None,
) -> CircuitBreaker:
    """获取或创建指定名称的熔断器（单例）。

    首次调用时创建实例并注册，后续调用返回同一实例。
    已存在的实例忽略新参数（保持一致性）。

    Args:
        name: 熔断器名称（如 "dashscope", "milvus", "opensearch"）。
        failure_threshold: 连续失败触发熔断的次数。
        recovery_timeout: OPEN → HALF_OPEN 冷却时间（秒）。
        half_open_max_calls: 半开状态最多探测请求数。

    Returns:
        CircuitBreaker 实例。
    """
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold or 5,
            recovery_timeout=recovery_timeout or 30.0,
            half_open_max_calls=half_open_max_calls or 1,
        )
    return _breakers[name]


def get_all_circuit_status() -> dict[str, dict[str, Any]]:
    """获取所有熔断器的状态快照（供 API 查询）。"""
    return {name: cb.get_status() for name, cb in _breakers.items()}


def reset_all_circuit_breakers() -> None:
    """重置所有熔断器到 CLOSED 状态（供测试和管理 API 使用）。"""
    for cb in _breakers.values():
        with cb._lock:
            cb.state = CircuitState.CLOSED
            cb.failure_count = 0
            cb.half_open_calls = 0
            cb.last_failure_time = 0.0


# ------------------------------------------------------------------
# 装饰器 — 声明式熔断保护
# ------------------------------------------------------------------

def circuit_call(
    name: str,
    failure_threshold: int | None = None,
    recovery_timeout: float | None = None,
    half_open_max_calls: int | None = None,
) -> Callable[[F], F]:
    """熔断保护装饰器 — 自动包装异步/同步函数。

    被装饰的函数在调用前检查熔断状态，调用后记录成功/失败。
    熔断开启时抛出 CircuitBreakerOpenError，不执行实际调用。

    Args:
        name: 熔断器名称。
        failure_threshold: 连续失败触发熔断的次数。
        recovery_timeout: 冷却时间（秒）。
        half_open_max_calls: 半开探测数。

    Returns:
        装饰后的函数。

    使用示例::

        @circuit_call("dashscope")
        async def call_llm(prompt: str) -> str:
            return await client.chat(prompt)
    """

    # 注册熔断器实例
    get_circuit_breaker(name, failure_threshold, recovery_timeout, half_open_max_calls)

    def decorator(fn: F) -> F:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                cb = get_circuit_breaker(name)
                async with cb:
                    return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                cb = get_circuit_breaker(name)
                cb._check_and_enter()
                try:
                    result = fn(*args, **kwargs)
                    cb._record_success()
                    return result
                except Exception:
                    cb._record_failure()
                    raise

            return sync_wrapper  # type: ignore[return-value]

    return decorator
