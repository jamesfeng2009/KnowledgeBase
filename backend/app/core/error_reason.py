"""
错误分类受控词汇表 — 跨层（LLM / HTTP / Tool / Runtime）统一错误归一化。

设计动机：
    跨层错误形态五花八门（OpenAI SDK / Anthropic SDK / httpx / 业务异常），
    下游（重试 / 熔断 / 日志 / 告警）不应各自识别 SDK 私有异常结构。
    本模块定义受控词汇表 ErrorReason + 归一化对象 ClassifiedError，
    作为所有 Provider / Tool / Runtime 异常的统一中间态。

    下游只需关心 ClassifiedError.retryable 字段决定是否重试，不需要懂 SDK 细节。

安全默认：
    UNKNOWN 默认不可重试 —— 看不懂的错误不盲目重试，避免对未知故障放大流量。
    这是生产系统的黄金法则。

QUOTA_EXHAUSTED vs RATE_LIMITED：
    HTTP 429 本身歧义。配额耗尽（不可恢复，需充值）和限流（可恢复，等一会就好）
    是不同语义，不能混。靠消息关键词区分 —— SDK 没给结构化字段的无奈之举。

使用示例::

    from app.core.error_reason import classify_error, ErrorLayer

    try:
        resp = await anthropic_client.messages.create(...)
    except Exception as exc:
        classified = classify_error(exc, layer=ErrorLayer.LLM, provider="anthropic")
        if classified.retryable:
            # 按 Retry-After 或指数退避重试
            ...
        else:
            # 不可重试，直接返回错误给用户或走降级路径
            ...

遵循单一职责：本模块仅提供错误分类与归一化，不含重试逻辑。
遵循依赖倒置：不 import 任何 LLM SDK，纯函数实现。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# ------------------------------------------------------------------
# 错误词汇表
# ------------------------------------------------------------------

class ErrorReason(str, Enum):
    """受控错误原因枚举 —— 按业务语义分类，可直接 JSON 序列化。

    7 大类组织：
        - auth: 认证 / 计费类（不可恢复）
        - quota: 配额 / 限流类（配额不可恢复，限流可恢复）
        - server: 服务端错误（5xx，通常可重试）
        - transport: 传输层错误（连接 / 超时，可重试）
        - payload: 请求载荷错误（context 溢出 / 内容违规 / 模型不存在）
        - runtime: 运行时自产终止信号（预算耗尽 / 死循环）
        - catch-all: 未知错误
    """

    # --- auth / billing ---
    AUTH_INVALID = "auth_invalid"          # API key 失效 / 认证失败
    BILLING = "billing"                    # 账单欠费 / 信用卡失效

    # --- quota / rate limit ---
    QUOTA_EXHAUSTED = "quota_exhausted"    # 配额耗尽（需充值，不可恢复）
    RATE_LIMITED = "rate_limited"          # 限流（等一会就好，可恢复）

    # --- server errors ---
    SERVER_ERROR = "server_error"          # 500 内部错误
    BAD_GATEWAY = "bad_gateway"            # 502 网关错误
    SERVICE_UNAVAILABLE = "service_unavailable"  # 503 服务不可用 / overloaded

    # --- transport ---
    CONNECT_ERROR = "connect_error"        # 连接失败 / DNS / 拒连
    TIMEOUT = "timeout"                   # 请求超时

    # --- payload ---
    CONTEXT_OVERFLOW = "context_overflow"  # 上下文超长
    CONTENT_BLOCKED = "content_blocked"    # 内容策略拦截 / safety
    MODEL_NOT_FOUND = "model_not_found"    # 模型名错误 / 不存在
    FORMAT_ERROR = "format_error"          # 请求格式错误 / 参数非法

    # --- runtime ---
    BUDGET_EXCEEDED = "budget_exceeded"    # 预算耗尽（运行时自产）
    RUNTIME_LOOP_DETECTED = "runtime_loop_detected"  # 死循环检测（运行时自产）
    TOOL_NOT_AVAILABLE = "tool_not_available"  # 工具不可用 / 熔断中

    # --- catch-all ---
    UNKNOWN = "unknown"                    # 未知错误（默认不可重试）


class ErrorLayer(str, Enum):
    """错误来源层 —— 决定 classify_error 的分派路径。"""

    LLM = "llm"          # LLM Provider 异常
    TOOL = "tool"        # 工具调用异常
    HTTP = "http"        # HTTP 应用层异常（有 status_code）
    TRANSPORT = "transport"  # 传输层异常（连接 / 超时 / 网络）
    RUNTIME = "runtime"  # 运行时自产异常


# ------------------------------------------------------------------
# 不可重试原因清单 —— 显式列出，安全默认
# ------------------------------------------------------------------

NON_RETRYABLE_REASONS: frozenset[ErrorReason] = frozenset({
    ErrorReason.AUTH_INVALID,
    ErrorReason.BILLING,
    ErrorReason.QUOTA_EXHAUSTED,
    ErrorReason.CONTENT_BLOCKED,
    ErrorReason.MODEL_NOT_FOUND,
    ErrorReason.FORMAT_ERROR,
    ErrorReason.BUDGET_EXCEEDED,
    ErrorReason.RUNTIME_LOOP_DETECTED,
    ErrorReason.TOOL_NOT_AVAILABLE,
    ErrorReason.UNKNOWN,  # ← 安全默认：未知错误不盲目重试
})


# ------------------------------------------------------------------
# 归一化错误对象
# ------------------------------------------------------------------

@dataclass
class ClassifiedError:
    """跨层归一化的错误对象 —— 所有 Provider / Tool / Runtime 异常的统一中间态。

    下游只需看 retryable 字段决定是否重试，不需要懂 SDK 私有异常结构。
    可序列化为 dict，便于写入 event log / LangFuse span / 告警系统。
    """

    reason: ErrorReason              # 归一化原因
    code: str                         # 原始错误码 / 异常类名
    retryable: bool                   # 是否可重试
    status_code: int | None = None    # HTTP 状态码（如有）
    provider: str | None = None       # LLM provider 名（anthropic / openai / dashscope）
    model: str | None = None          # 模型名（如 claude-sonnet-4-6）
    raw_message: str = ""             # 原始错误消息（已截断）
    retry_after_seconds: float | None = None  # Retry-After header（秒）
    context: dict[str, Any] = field(default_factory=dict)  # 扩展上下文

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入日志 / event log 的 dict。"""
        d = asdict(self)
        d["reason"] = self.reason.value
        d["layer"] = self.context.get("layer")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ClassifiedError:
        """从 dict 反序列化（用于恢复场景）。"""
        reason_str = d.get("reason", "unknown")
        try:
            reason = ErrorReason(reason_str)
        except ValueError:
            reason = ErrorReason.UNKNOWN
        return cls(
            reason=reason,
            code=d.get("code", "unknown"),
            retryable=d.get("retryable", False),
            status_code=d.get("status_code"),
            provider=d.get("provider"),
            model=d.get("model"),
            raw_message=d.get("raw_message", ""),
            retry_after_seconds=d.get("retry_after_seconds"),
            context=d.get("context", {}),
        )


# ------------------------------------------------------------------
# HTTP 状态码 → ErrorReason 映射表
# ------------------------------------------------------------------

# 永久性错误状态码 —— 不可重试，直接抛
PERMANENT_STATUS_CODES: frozenset[int] = frozenset({
    400,  # Bad Request
    401,  # Unauthorized
    402,  # Payment Required
    403,  # Forbidden
    404,  # Not Found
    422,  # Unprocessable Entity
})

# 可重试状态码 —— 服务端临时故障或限流
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({
    408,  # Request Timeout
    425,  # Too Early
    429,  # Too Many Requests（需进一步区分 quota vs rate_limit）
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
    529,  # Provider Overloaded（Anthropic 专用，提示降级 fallback model）
})

# 状态码 → ErrorReason 默认映射（429 / 529 需二次识别，见 _classify_http）
_STATUS_REASON_MAP: dict[int, ErrorReason] = {
    400: ErrorReason.FORMAT_ERROR,
    401: ErrorReason.AUTH_INVALID,
    402: ErrorReason.BILLING,
    403: ErrorReason.AUTH_INVALID,
    404: ErrorReason.MODEL_NOT_FOUND,
    408: ErrorReason.TIMEOUT,
    422: ErrorReason.FORMAT_ERROR,
    429: ErrorReason.RATE_LIMITED,  # 默认限流，消息含 "quota" 时升级为 QUOTA_EXHAUSTED
    500: ErrorReason.SERVER_ERROR,
    502: ErrorReason.BAD_GATEWAY,
    503: ErrorReason.SERVICE_UNAVAILABLE,
    504: ErrorReason.TIMEOUT,
    529: ErrorReason.SERVICE_UNAVAILABLE,  # Overloaded —— 提示降级 fallback
}

# Retry-After header 的两种大小写写法
_RETRY_AFTER_HEADERS: tuple[str, ...] = ("retry-after", "Retry-After")
