"""
P0-1 错误分类层测试 —— 验证 classify_error 跨层归一化与 NON_RETRYABLE 安全默认。

测试覆盖：
    - 18 个 ErrorReason 枚举值 + 5 个 ErrorLayer
    - NON_RETRYABLE_REASONS 集合（UNKNOWN 默认不可重试）
    - HTTP 状态码映射（4xx 永久 / 5xx 可重试 / 429 配额 vs 限流 / 529 Overloaded）
    - LLM 层消息文本二次识别（context_overflow / content_blocked / model_not_found）
    - 传输层（timeout / connect_error）
    - 运行时自产异常（BudgetExceeded / InfiniteLoopDetected）
    - CancelledError 不吞信号
    - ClassifiedError 序列化 / 反序列化
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.error_classifier import classify_error, is_retryable
from app.core.error_reason import (
    ClassifiedError,
    ErrorLayer,
    ErrorReason,
    NON_RETRYABLE_REASONS,
    PERMANENT_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
)
from app.core.exceptions import (
    AgentError,
    BudgetExceeded,
    InfiniteLoopDetected,
    PromptInjectionDetected,
    SECURITY_VETO_EXCEPTIONS,
)


# ------------------------------------------------------------------
# 测试辅助：模拟各 SDK 异常形态
# ------------------------------------------------------------------

class FakeSDKError(Exception):
    """模拟 OpenAI / Anthropic SDK 异常 —— 带 status_code / response / message。"""

    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = response


class FakeResponse:
    """模拟 SDK 异常的 response 对象 —— 带 status_code / headers。"""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


# ------------------------------------------------------------------
# 枚举与集合测试
# ------------------------------------------------------------------

class TestErrorReasonEnum:
    """ErrorReason 枚举完整性测试。"""

    def test_all_reasons_have_string_value(self):
        """所有 ErrorReason 值必须是字符串。"""
        for reason in ErrorReason:
            assert isinstance(reason.value, str)
            assert reason.value  # 非空

    def test_reason_count(self):
        """枚举值数量符合预期（17 个，覆盖 7 大类）。"""
        assert len(list(ErrorReason)) == 17

    def test_unknown_in_non_retryable(self):
        """UNKNOWN 默认在不可重试集合 —— 安全默认。"""
        assert ErrorReason.UNKNOWN in NON_RETRYABLE_REASONS

    def test_budget_exceeded_non_retryable(self):
        """BUDGET_EXCEEDED 不可重试 —— 运行时自产信号。"""
        assert ErrorReason.BUDGET_EXCEEDED in NON_RETRYABLE_REASONS

    def test_rate_limited_retryable(self):
        """RATE_LIMITED 不在不可重试集合 —— 可重试。"""
        assert ErrorReason.RATE_LIMITED not in NON_RETRYABLE_REASONS

    def test_quota_exhausted_non_retryable(self):
        """QUOTA_EXHAUSTED 不可重试 —— 需充值。"""
        assert ErrorReason.QUOTA_EXHAUSTED in NON_RETRYABLE_REASONS

    def test_non_retryable_count(self):
        """不可重试原因数符合预期（10 个）。"""
        assert len(NON_RETRYABLE_REASONS) == 10


# ------------------------------------------------------------------
# HTTP 状态码分类测试
# ------------------------------------------------------------------

class TestHTTPClassification:
    """HTTP 状态码 → ErrorReason 映射测试。"""

    @pytest.mark.parametrize(
        "status,expected_reason,expected_retryable",
        [
            (400, ErrorReason.FORMAT_ERROR, False),
            (401, ErrorReason.AUTH_INVALID, False),
            (402, ErrorReason.BILLING, False),
            (403, ErrorReason.AUTH_INVALID, False),
            (404, ErrorReason.MODEL_NOT_FOUND, False),
            (422, ErrorReason.FORMAT_ERROR, False),
            (500, ErrorReason.SERVER_ERROR, True),
            (502, ErrorReason.BAD_GATEWAY, True),
            (503, ErrorReason.SERVICE_UNAVAILABLE, True),
            (504, ErrorReason.TIMEOUT, True),
        ],
    )
    def test_status_code_mapping(self, status, expected_reason, expected_retryable):
        """HTTP 状态码正确映射到 ErrorReason。"""
        exc = FakeSDKError(f"HTTP {status}", status_code=status)
        c = classify_error(exc, layer=ErrorLayer.HTTP)
        assert c.reason == expected_reason
        assert c.retryable == expected_retryable
        assert c.status_code == status

    def test_429_rate_limited_retryable(self):
        """429 默认为 RATE_LIMITED，可重试。"""
        exc = FakeSDKError("Rate limited, please retry", status_code=429)
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai")
        assert c.reason == ErrorReason.RATE_LIMITED
        assert c.retryable is True

    def test_429_quota_exhausted_non_retryable(self):
        """429 消息含 quota 关键词时升级为 QUOTA_EXHAUSTED，不可重试。"""
        exc = FakeSDKError(
            "You exceeded your current quota, please check your plan and billing details.",
            status_code=429,
        )
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai")
        assert c.reason == ErrorReason.QUOTA_EXHAUSTED
        assert c.retryable is False

    def test_429_insufficient_balance_non_retryable(self):
        """429 消息含 'insufficient balance' 也算配额耗尽。"""
        exc = FakeSDKError("insufficient balance", status_code=429)
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="dashscope")
        assert c.reason == ErrorReason.QUOTA_EXHAUSTED
        assert c.retryable is False

    def test_529_overloaded_retryable(self):
        """529 Overloaded 可重试，提示降级 fallback model。"""
        exc = FakeSDKError("Overloaded", status_code=529)
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="anthropic")
        assert c.reason == ErrorReason.SERVICE_UNAVAILABLE
        assert c.retryable is True

    def test_retry_after_header_extracted(self):
        """Retry-After header 被正确提取为秒数。"""
        resp = FakeResponse(429, {"retry-after": "5"})
        exc = FakeSDKError("Rate limited", status_code=429, response=resp)
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai")
        assert c.retry_after_seconds == 5.0

    def test_retry_after_capped_at_max(self):
        """Retry-After 受 max_delay 封顶，防止异常 header 导致长等待。"""
        resp = FakeResponse(429, {"retry-after": "99999"})
        exc = FakeSDKError("Rate limited", status_code=429, response=resp)
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai")
        assert c.retry_after_seconds is not None
        assert c.retry_after_seconds <= 60.0  # 封顶

    def test_retry_after_capital_header(self):
        """Retry-After 大写 header 名也能提取。"""
        resp = FakeResponse(429, {"Retry-After": "3"})
        exc = FakeSDKError("Rate limited", status_code=429, response=resp)
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai")
        assert c.retry_after_seconds == 3.0


# ------------------------------------------------------------------
# LLM 层消息文本二次识别测试
# ------------------------------------------------------------------

class TestLLMClassification:
    """LLM 层在 HTTP 基础上的消息文本二次识别测试。"""

    def test_context_overflow_detected(self):
        """context_length 关键词触发 CONTEXT_OVERFLOW，不可重试。"""
        exc = FakeSDKError(
            "context_length_exceeded: your input exceeds the maximum context length",
            status_code=400,
        )
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="anthropic", model="claude")
        assert c.reason == ErrorReason.CONTEXT_OVERFLOW
        assert c.retryable is False

    def test_content_blocked_detected(self):
        """content_policy 关键词触发 CONTENT_BLOCKED，不可重试。"""
        exc = FakeSDKError(
            "content_policy_violation: safety filter triggered",
            status_code=400,
        )
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai")
        assert c.reason == ErrorReason.CONTENT_BLOCKED
        assert c.retryable is False

    def test_model_not_found_detected(self):
        """model_not_found 关键词触发 MODEL_NOT_FOUND，不可重试。"""
        exc = FakeSDKError(
            "The model 'gpt-5' does not exist or you do not have access to it.",
            status_code=404,
        )
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai")
        assert c.reason == ErrorReason.MODEL_NOT_FOUND
        assert c.retryable is False

    def test_provider_model_attached(self):
        """LLM 层分类结果携带 provider / model。"""
        exc = FakeSDKError("Server error", status_code=500)
        c = classify_error(
            exc, layer=ErrorLayer.LLM, provider="anthropic", model="claude-sonnet-4-6"
        )
        assert c.provider == "anthropic"
        assert c.model == "claude-sonnet-4-6"


# ------------------------------------------------------------------
# 传输层与运行时测试
# ------------------------------------------------------------------

class TestTransportClassification:
    """传输层异常分类测试。"""

    def test_timeout_retryable(self):
        """TimeoutError 可重试。"""
        exc = asyncio.TimeoutError("Request timed out")
        c = classify_error(exc, layer=ErrorLayer.TRANSPORT)
        assert c.reason == ErrorReason.TIMEOUT
        assert c.retryable is True

    def test_connection_error_retryable(self):
        """ConnectionError 可重试。"""
        exc = ConnectionError("Connection refused")
        c = classify_error(exc, layer=ErrorLayer.TRANSPORT)
        assert c.reason == ErrorReason.CONNECT_ERROR
        assert c.retryable is True


class TestRuntimeClassification:
    """运行时自产异常分类测试。"""

    def test_budget_exceeded_non_retryable(self):
        """BudgetExceeded 不可重试。"""
        exc = BudgetExceeded(axis="tokens", value=150000, limit=100000, run_id="run-123")
        c = classify_error(exc, layer=ErrorLayer.RUNTIME)
        assert c.reason == ErrorReason.BUDGET_EXCEEDED
        assert c.retryable is False

    def test_infinite_loop_non_retryable(self):
        """InfiniteLoopDetected 不可重试。"""
        exc = InfiniteLoopDetected("same tool_call x3", window=10)
        c = classify_error(exc, layer=ErrorLayer.RUNTIME)
        assert c.reason == ErrorReason.RUNTIME_LOOP_DETECTED
        assert c.retryable is False

    def test_runtime_exception_carries_context(self):
        """BudgetExceeded 异常携带结构化 context。"""
        exc = BudgetExceeded(axis="cost_usd", value=1.5, limit=1.0, run_id="run-456")
        d = exc.to_dict()
        assert d["error_type"] == "BudgetExceeded"
        assert d["context"]["axis"] == "cost_usd"
        assert d["context"]["run_id"] == "run-456"


# ------------------------------------------------------------------
# CancelledError 与兜底测试
# ------------------------------------------------------------------

class TestCancelledAndUnknown:
    """CancelledError 与未知异常处理测试。"""

    def test_cancelled_error_marked_non_retryable(self):
        """asyncio.CancelledError 标记为不可重试 —— 不吞 cancel 信号。"""
        exc = asyncio.CancelledError()
        c = classify_error(exc, layer=ErrorLayer.LLM)
        assert c.retryable is False
        assert c.context.get("cancelled") is True

    def test_unknown_exception_non_retryable(self):
        """未知异常默认不可重试 —— 安全默认。"""
        exc = ValueError("Some weird error")
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="anthropic")
        assert c.reason == ErrorReason.UNKNOWN
        assert c.retryable is False

    def test_is_retryable_helper(self):
        """is_retryable 便捷函数行为正确。"""
        assert is_retryable(asyncio.TimeoutError(), layer=ErrorLayer.TRANSPORT) is True
        assert is_retryable(ValueError("err"), layer=ErrorLayer.LLM) is False


# ------------------------------------------------------------------
# 序列化测试
# ------------------------------------------------------------------

class TestSerialization:
    """ClassifiedError 序列化 / 反序列化测试。"""

    def test_to_dict_contains_all_fields(self):
        """to_dict 包含所有字段。"""
        exc = FakeSDKError("Rate limited", status_code=429)
        c = classify_error(exc, layer=ErrorLayer.LLM, provider="openai", model="gpt-4")
        d = c.to_dict()
        assert "reason" in d
        assert "retryable" in d
        assert "status_code" in d
        assert "provider" in d
        assert "model" in d
        assert "layer" in d
        assert d["reason"] == "rate_limited"

    def test_from_dict_roundtrip(self):
        """from_dict 能还原 to_dict 的结果。"""
        exc = FakeSDKError("Server error", status_code=500)
        original = classify_error(exc, layer=ErrorLayer.LLM, provider="anthropic", model="claude")
        d = original.to_dict()
        restored = ClassifiedError.from_dict(d)
        assert restored.reason == original.reason
        assert restored.retryable == original.retryable
        assert restored.status_code == original.status_code
        assert restored.provider == original.provider

    def test_from_dict_unknown_reason_fallback(self):
        """from_dict 遇到未知 reason 字符串时回退到 UNKNOWN。"""
        d = {"reason": "nonexistent_reason", "code": "X", "retryable": False}
        restored = ClassifiedError.from_dict(d)
        assert restored.reason == ErrorReason.UNKNOWN


# ------------------------------------------------------------------
# 异常基类与安全否决测试
# ------------------------------------------------------------------

class TestExceptionsModule:
    """app.core.exceptions 异常层级测试。"""

    def test_agent_error_carries_context(self):
        """AgentError 基类携带结构化 context。"""
        err = AgentError("test error", key1="value1", key2=42)
        assert err.context["key1"] == "value1"
        assert err.context["key2"] == 42

    def test_budget_exceeded_is_agent_error(self):
        """BudgetExceeded 是 AgentError 子类。"""
        err = BudgetExceeded(axis="tokens", value=100, limit=50)
        assert isinstance(err, AgentError)

    def test_prompt_injection_is_security_veto(self):
        """PromptInjectionDetected 在 SECURITY_VETO_EXCEPTIONS 集合。"""
        err = PromptInjectionDetected("user_input", pattern="ignore prior instructions")
        assert isinstance(err, SECURITY_VETO_EXCEPTIONS)

    def test_security_veto_exception_count(self):
        """SECURITY_VETO_EXCEPTIONS 含 3 个安全异常。"""
        assert len(SECURITY_VETO_EXCEPTIONS) == 3
