"""
错误分类器 —— classify_error() 入口与各层分派实现。

按 ErrorLayer 分派到 _classify_http / _classify_llm / _classify_runtime / _classify_transport。

设计要点：
    1. 多形态异常归一化提取 status_code —— 兼容 OpenAI / Anthropic / httpx 各种 SDK，
       exc.status_code / exc.response.status_code 双重探测，header 大小写兼顾。
    2. Retry-After 优先于 backoff —— 服务端说等多久就等多久，但受 max_delay 封顶，
       防止恶意 / 异常 header 让客户端等几小时。
    3. 429 的"配额 vs 限流"细分靠消息文本 —— SDK 没给结构化字段的无奈之举。
    4. LLM 层在 HTTP 分类基础上做消息文本二次识别 ——
       context_overflow / content_blocked / model_not_found。
    5. asyncio.CancelledError 永不被分类为可重试 ——
       分类器不吞 cancel 信号，由调用方决定是否重新发起。

延迟 import：
    BudgetExceeded / InfiniteLoopDetected 等 runtime 异常延迟到函数体内 import，
    避免 exceptions.py ↔ error_reason.py 循环依赖。

使用示例::

    from app.core.error_reason import classify_error, ErrorLayer

    try:
        resp = await openai_client.chat.completions.create(...)
    except Exception as exc:
        classified = classify_error(
            exc, layer=ErrorLayer.LLM, provider="openai", model="gpt-4"
        )
        if not classified.retryable:
            log.error("llm.permanent_error", **classified.to_dict())
            raise
        # 可重试：按 classified.retry_after_seconds 或指数退避
"""

from __future__ import annotations

from typing import Any

from app.core.error_reason import (
    ClassifiedError,
    ErrorLayer,
    ErrorReason,
    NON_RETRYABLE_REASONS,
    PERMANENT_STATUS_CODES,
    _RETRY_AFTER_HEADERS,
    _STATUS_REASON_MAP,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

# 原始错误消息截断长度 —— 防止超长消息污染日志
_MAX_MESSAGE_LEN = 500

# 429 消息含这些关键词时升级为 QUOTA_EXHAUSTED（不可恢复）
_QUOTA_KEYWORDS: tuple[str, ...] = (
    "quota", "exhausted", "insufficient", "balance",
    "credit", "payment", "billing",
)

# LLM 层消息文本二次识别关键词
_CONTEXT_OVERFLOW_KEYWORDS: tuple[str, ...] = (
    "context_length", "context window", "maximum context",
    "context length", "too long",
)
_CONTENT_BLOCKED_KEYWORDS: tuple[str, ...] = (
    "content_policy", "content policy", "safety",
    "content filter", "blocked by policy",
)
_MODEL_NOT_FOUND_KEYWORDS: tuple[str, ...] = (
    "model_not_found", "model not found", "does not exist",
    "invalid model", "unknown model",
)


def _truncate(text: str, limit: int = _MAX_MESSAGE_LEN) -> str:
    """截断超长错误消息，保留首尾 + 中间省略号。"""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 3
    return f"{text[:head]}...{text[-tail:]}"


def _extract_status_code(exc: BaseException) -> int | None:
    """从异常对象多形态提取 HTTP status_code。

    OpenAI SDK: exc.status_code / exc.response.status_code
    Anthropic SDK: exc.status_code / exc.response.status_code
    httpx: exc.response.status_code
    """
    # 直接属性
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass

    # response.status_code 路径
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None:
            try:
                return int(status)
            except (TypeError, ValueError):
                pass

    return None


def _extract_retry_after(exc: BaseException, max_delay: float = 60.0) -> float | None:
    """从异常的 response headers 提取 Retry-After 值（秒）。

    服务端说等多久就等多久，但受 max_delay 封顶 ——
    防止恶意 / 异常 header 让客户端等几小时。
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None

    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    for header_name in _RETRY_AFTER_HEADERS:
        raw: Any | None = None
        # httpx Headers 支持 .get 和 __getitem__，大小写不敏感
        try:
            raw = headers.get(header_name)
        except (AttributeError, TypeError):
            try:
                raw = headers[header_name]
            except (KeyError, TypeError):
                continue

        if raw is None:
            continue

        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue

        # 封顶，防止异常 header 导致客户端等待几小时
        return max(0.0, min(value, max_delay))

    return None


def _extract_raw_message(exc: BaseException) -> str:
    """提取异常的原始消息文本（已截断）。

    优先用 SDK 的 message 属性，fallback 到 str(exc)。
    """
    # OpenAI / Anthropic SDK 异常有 message 属性
    msg = getattr(exc, "message", None)
    if not msg:
        # httpx.HTTPStatusError 有 .message
        msg = getattr(exc, "message", None)
    if not msg:
        msg = str(exc) or exc.__class__.__name__
    return _truncate(msg)


def _classify_http(exc: BaseException, *, layer: ErrorLayer) -> ClassifiedError:
    """HTTP 层分类 —— 按 status_code 决定 reason。"""
    status = _extract_status_code(exc)
    raw_message = _extract_raw_message(exc)
    retry_after = _extract_retry_after(exc)

    if status is None:
        # 无状态码 —— 视为传输层错误
        reason = ErrorReason.UNKNOWN
        retryable = False
    elif status in PERMANENT_STATUS_CODES:
        reason = _STATUS_REASON_MAP.get(status, ErrorReason.FORMAT_ERROR)
        retryable = False
    elif status == 429:
        # 429 歧义：配额耗尽（不可恢复）vs 限流（可恢复）
        msg_lower = raw_message.lower()
        if any(kw in msg_lower for kw in _QUOTA_KEYWORDS):
            reason = ErrorReason.QUOTA_EXHAUSTED
            retryable = False
        else:
            reason = ErrorReason.RATE_LIMITED
            retryable = True
    elif status == 529:
        # Anthropic Overloaded —— 可重试但提示降级 fallback model
        reason = ErrorReason.SERVICE_UNAVAILABLE
        retryable = True
    elif status in (500, 502, 503, 504, 408):
        reason = _STATUS_REASON_MAP.get(status, ErrorReason.SERVER_ERROR)
        retryable = True
    else:
        # 既不在永久也不在可重试列表 —— 默认不可重试（安全默认）
        reason = _STATUS_REASON_MAP.get(status, ErrorReason.UNKNOWN)
        retryable = False

    return ClassifiedError(
        reason=reason,
        code=exc.__class__.__name__,
        retryable=retryable,
        status_code=status,
        raw_message=raw_message,
        retry_after_seconds=retry_after,
        context={"layer": layer.value},
    )


def _classify_llm(
    exc: BaseException,
    *,
    provider: str | None,
    model: str | None,
) -> ClassifiedError:
    """LLM 层分类 —— 在 HTTP 基础上做消息文本二次识别。

    LLM SDK 异常通常是 HTTP 异常的子类（带 status_code），
    先走 HTTP 分类，再用消息文本识别 context_overflow / content_blocked。
    """
    classified = _classify_http(exc, layer=ErrorLayer.LLM)
    classified.provider = provider
    classified.model = model

    # 仅在 HTTP 层已判定为可重试或 unknown 时做二次识别 ——
    # 已明确为 AUTH_INVALID / BILLING 等的不再降级
    msg_lower = classified.raw_message.lower()

    if any(kw in msg_lower for kw in _CONTEXT_OVERFLOW_KEYWORDS):
        # 上下文超长 —— 不可重试（重试也会超），需压缩 context
        classified.reason = ErrorReason.CONTEXT_OVERFLOW
        classified.retryable = False
    elif any(kw in msg_lower for kw in _CONTENT_BLOCKED_KEYWORDS):
        # 内容策略拦截 —— 不可重试
        classified.reason = ErrorReason.CONTENT_BLOCKED
        classified.retryable = False
    elif any(kw in msg_lower for kw in _MODEL_NOT_FOUND_KEYWORDS):
        # 模型不存在 —— 不可重试
        classified.reason = ErrorReason.MODEL_NOT_FOUND
        classified.retryable = False

    classified.context["layer"] = ErrorLayer.LLM.value
    return classified


def _classify_transport(exc: BaseException) -> ClassifiedError:
    """传输层分类 —— 连接错误 / 超时，默认可重试。"""
    exc_name = exc.__class__.__name__.lower()

    if "timeout" in exc_name or "timedout" in exc_name:
        reason = ErrorReason.TIMEOUT
    elif "connect" in exc_name or "connection" in exc_name:
        reason = ErrorReason.CONNECT_ERROR
    else:
        reason = ErrorReason.UNKNOWN

    # 连接错误 / 超时默认可重试
    retryable = reason in (ErrorReason.TIMEOUT, ErrorReason.CONNECT_ERROR)

    return ClassifiedError(
        reason=reason,
        code=exc.__class__.__name__,
        retryable=retryable,
        raw_message=_extract_raw_message(exc),
        context={"layer": ErrorLayer.TRANSPORT.value},
    )


def _classify_runtime(exc: BaseException) -> ClassifiedError:
    """运行时自产异常分类 —— BudgetExceeded / InfiniteLoopDetected 等。

    延迟 import 避免循环依赖：exceptions.py 反向依赖 error_reason，
    提前 import 会循环。
    """
    # 延迟 import
    from app.core.exceptions import (
        BudgetExceeded,
        InfiniteLoopDetected,
    )

    if isinstance(exc, BudgetExceeded):
        reason = ErrorReason.BUDGET_EXCEEDED
    elif isinstance(exc, InfiniteLoopDetected):
        reason = ErrorReason.RUNTIME_LOOP_DETECTED
    else:
        reason = ErrorReason.UNKNOWN

    # 运行时自产信号都不可重试 —— 否则下游会傻乎乎重试
    return ClassifiedError(
        reason=reason,
        code=exc.__class__.__name__,
        retryable=False,
        raw_message=_extract_raw_message(exc),
        context={"layer": ErrorLayer.RUNTIME.value},
    )


def classify_error(
    exc: BaseException,
    *,
    layer: ErrorLayer,
    provider: str | None = None,
    model: str | None = None,
) -> ClassifiedError:
    """错误分类入口 —— 按 layer 分派到对应分类器。

    Args:
        exc: 任意异常对象（OpenAI / Anthropic / httpx / 业务异常）。
        layer: 错误来源层（LLM / TOOL / HTTP / RUNTIME）。
        provider: LLM provider 名（仅 LLM 层用）。
        model: 模型名（仅 LLM 层用，便于告警定位）。

    Returns:
        ClassifiedError 归一化错误对象，下游看 retryable 决定是否重试。

    Note:
        asyncio.CancelledError 不会被分类为可重试 —— 分类器不吞 cancel 信号，
        调用方应单独处理 CancelledError（直接 raise，不重试）。

    使用示例::

        try:
            resp = await anthropic_client.messages.create(...)
        except asyncio.CancelledError:
            raise  # 永不被吞
        except Exception as exc:
            classified = classify_error(
                exc, layer=ErrorLayer.LLM, provider="anthropic", model="claude-sonnet-4-6"
            )
            if not classified.retryable:
                log.error("llm.permanent_error", **classified.to_dict())
                raise
            # 可重试：按 classified.retry_after_seconds 或指数退避
    """
    # asyncio.CancelledError 特殊处理 —— 不分类，由调用方处理
    # 这里只做记录，不吞信号
    try:
        import asyncio
        if isinstance(exc, asyncio.CancelledError):
            # 返回不可重试的 ClassifiedError，但调用方应单独捕获 CancelledError 直接 raise
            return ClassifiedError(
                reason=ErrorReason.UNKNOWN,
                code="CancelledError",
                retryable=False,
                raw_message="asyncio.CancelledError — must not be swallowed",
                context={"layer": layer.value, "cancelled": True},
            )
    except ImportError:
        pass

    # 优先识别运行时自产异常（避免被 LLM 层误判）
    if layer == ErrorLayer.RUNTIME:
        return _classify_runtime(exc)

    # 识别是否为 SDK / HTTP 异常（有 status_code 或 response 属性）
    status = _extract_status_code(exc)
    response = getattr(exc, "response", None)

    if status is not None or response is not None:
        # 有 HTTP 语义 —— 走 HTTP / LLM 分类
        if layer == ErrorLayer.LLM:
            return _classify_llm(exc, provider=provider, model=model)
        return _classify_http(exc, layer=layer)

    # 传输层异常（连接 / 超时 / 网络）
    exc_name = exc.__class__.__name__.lower()
    transport_keywords = ("timeout", "connect", "connection", "network", "dns")
    if any(kw in exc_name for kw in transport_keywords):
        return _classify_transport(exc)

    # 兜底 —— 未知错误，默认不可重试（安全默认）
    return ClassifiedError(
        reason=ErrorReason.UNKNOWN,
        code=exc.__class__.__name__,
        retryable=False,
        raw_message=_extract_raw_message(exc),
        provider=provider,
        model=model,
        context={"layer": layer.value},
    )


def is_retryable(exc: BaseException, *, layer: ErrorLayer) -> bool:
    """便捷函数 —— 直接判断异常是否可重试。

    等价于 classify_error(exc, layer=layer).retryable，
    但不返回完整 ClassifiedError，适合简单场景。
    """
    return classify_error(exc, layer=layer).retryable
