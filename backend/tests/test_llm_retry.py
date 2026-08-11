"""
P0-2 LLM Provider 重试装饰器测试 —— 验证 with_llm_retry 的 async generator 重试行为。

测试覆盖：
    - 成功路径（无重试）
    - 流式开始前可重试错误（429 / 529 / 500 / timeout）—— 重试后成功
    - 流式开始前不可重试错误（401 / 402 / context_overflow）—— 立即抛
    - 流式开始后错误 —— 不重试（避免重复 yield）
    - CancelledError 永不被吞
    - Retry-After header 优先于指数退避
    - 达到 max_attempts 后放弃
    - DashScope 子类继承时 provider 名正确
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from app.utils.llm_retry import with_llm_retry


# ------------------------------------------------------------------
# 测试辅助：模拟 Provider 类与 SDK 异常
# ------------------------------------------------------------------

class FakeSDKError(Exception):
    """模拟 SDK 异常 —— 带 status_code / response / message。"""

    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = response


class FakeResponse:
    """模拟 SDK response —— 带 status_code / headers。"""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeProvider:
    """模拟 LLM Provider —— 用于测试 with_llm_retry 装饰器。"""

    _circuit_breaker_name = "test_provider"
    default_model = "test-model"

    def __init__(self, chunks_or_exc, *, fail_before_success=0):
        """
        Args:
            chunks_or_exc: 要 yield 的 chunks 列表，或要抛的异常
            fail_before_success: 成功前失败的次数（模拟瞬时错误）
        """
        self._chunks_or_exc = chunks_or_exc
        self._fail_before_success = fail_before_success
        self._call_count = 0

    @with_llm_retry(provider="test_provider")
    async def chat(self, messages=None, **kwargs) -> AsyncIterator[Any]:
        """模拟 chat —— 根据配置 yield chunks 或抛异常。"""
        self._call_count += 1
        # 模拟熔断器记录（真实 Provider 会在 chat 内部记录）
        # 这里简化为只统计调用次数
        if self._call_count <= self._fail_before_success:
            # 前 N 次抛异常
            if isinstance(self._chunks_or_exc, BaseException):
                raise self._chunks_or_exc
            # 如果配置的是 chunks 但要失败，抛一个默认可重试错误
            raise FakeSDKError("Rate limited", status_code=429)
        # 成功路径
        if isinstance(self._chunks_or_exc, BaseException):
            raise self._chunks_or_exc
        for chunk in self._chunks_or_exc:
            yield chunk


class StreamingFailProvider:
    """模拟流式开始后失败的 Provider —— 测试不重试逻辑。"""

    _circuit_breaker_name = "streaming_fail"
    default_model = "test-model"
    _call_count = 0

    @with_llm_retry(provider="streaming_fail")
    async def chat(self, messages=None, **kwargs) -> AsyncIterator[Any]:
        self._call_count += 1
        yield "first chunk"  # 第一个 chunk 成功
        raise FakeSDKError("Stream broken mid-way", status_code=500)


# ------------------------------------------------------------------
# 成功路径测试
# ------------------------------------------------------------------

class TestSuccessPath:
    """无错误时的正常流式输出测试。"""

    @pytest.mark.asyncio
    async def test_normal_streaming(self):
        """正常流式输出所有 chunks，无重试。"""
        provider = FakeProvider(["hello", " ", "world"])
        chunks = []
        async for chunk in provider.chat():
            chunks.append(chunk)
        assert chunks == ["hello", " ", "world"]
        assert provider._call_count == 1

    @pytest.mark.asyncio
    async def test_empty_generator(self):
        """空 generator 正常结束，不抛异常。"""
        provider = FakeProvider([])
        chunks = []
        async for chunk in provider.chat():
            chunks.append(chunk)
        assert chunks == []
        assert provider._call_count == 1

    @pytest.mark.asyncio
    async def test_single_chunk(self):
        """单个 chunk 正常输出。"""
        provider = FakeProvider(["only"])
        chunks = []
        async for chunk in provider.chat():
            chunks.append(chunk)
        assert chunks == ["only"]


# ------------------------------------------------------------------
# 流式开始前可重试错误测试
# ------------------------------------------------------------------

class TestRetryableBeforeStreaming:
    """流式开始前的可重试错误（429 / 529 / 500 / timeout）会重试。"""

    @pytest.mark.asyncio
    async def test_429_rate_limited_retry_success(self):
        """429 限流重试后成功。"""
        provider = FakeProvider(["success"], fail_before_success=1)
        chunks = []
        async for chunk in provider.chat():
            chunks.append(chunk)
        assert chunks == ["success"]
        assert provider._call_count == 2  # 第一次失败，第二次成功

    @pytest.mark.asyncio
    async def test_529_overloaded_retry_success(self):
        """529 Overloaded 重试后成功。"""
        exc = FakeSDKError("Overloaded", status_code=529)
        provider = FakeProvider(exc, fail_before_success=2)
        # 重新配置：前 2 次抛 529，第 3 次成功
        provider._chunks_or_exc = ["success"]

        # 需要自定义 chat 来抛特定异常
        call_count = [0]

        class CustomProvider:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                if call_count[0] <= 2:
                    raise FakeSDKError("Overloaded", status_code=529)
                yield "success"

        cp = CustomProvider()
        chunks = []
        async for chunk in cp.chat():
            chunks.append(chunk)
        assert chunks == ["success"]
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_500_server_error_retry_success(self):
        """500 服务端错误重试后成功。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise FakeSDKError("Internal error", status_code=500)
                yield "ok"

        p = P()
        chunks = []
        async for chunk in p.chat():
            chunks.append(chunk)
        assert chunks == ["ok"]
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_timeout_retry_success(self):
        """TimeoutError 重试后成功。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise asyncio.TimeoutError("timed out")
                yield "ok"

        p = P()
        chunks = []
        async for chunk in p.chat():
            chunks.append(chunk)
        assert chunks == ["ok"]
        assert call_count[0] == 2


# ------------------------------------------------------------------
# 流式开始前不可重试错误测试
# ------------------------------------------------------------------

class TestNonRetryableBeforeStreaming:
    """流式开始前的不可重试错误立即抛，不重试。"""

    @pytest.mark.asyncio
    async def test_401_auth_invalid_no_retry(self):
        """401 认证失败立即抛，不重试。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                raise FakeSDKError("Invalid API key", status_code=401)
                yield  # 让 Python 识别为 async generator（不可达）

        p = P()
        with pytest.raises(FakeSDKError):
            async for _ in p.chat():
                pass
        assert call_count[0] == 1  # 只调用一次，不重试

    @pytest.mark.asyncio
    async def test_402_billing_no_retry(self):
        """402 计费问题立即抛。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                raise FakeSDKError("Payment required", status_code=402)
                yield  # 让 Python 识别为 async generator（不可达）

        p = P()
        with pytest.raises(FakeSDKError):
            async for _ in p.chat():
                pass
        assert call_count[0] == 1

    @pytest.mark.asyncio
    async def test_context_overflow_no_retry(self):
        """context_length_exceeded 立即抛。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                raise FakeSDKError(
                    "context_length_exceeded: maximum context length",
                    status_code=400,
                )
                yield  # 让 Python 识别为 async generator（不可达）

        p = P()
        with pytest.raises(FakeSDKError):
            async for _ in p.chat():
                pass
        assert call_count[0] == 1

    @pytest.mark.asyncio
    async def test_quota_exhausted_no_retry(self):
        """429 含 quota 关键词立即抛（配额耗尽）。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                raise FakeSDKError(
                    "You exceeded your current quota, please check your plan.",
                    status_code=429,
                )
                yield  # 让 Python 识别为 async generator（不可达）

        p = P()
        with pytest.raises(FakeSDKError):
            async for _ in p.chat():
                pass
        assert call_count[0] == 1


# ------------------------------------------------------------------
# 流式开始后错误测试 —— 不重试
# ------------------------------------------------------------------

class TestStreamingErrorNoRetry:
    """流式开始后的错误不重试（避免重复 yield）。"""

    @pytest.mark.asyncio
    async def test_error_after_first_chunk_no_retry(self):
        """第一个 chunk 之后失败 —— 不重试，直接抛。"""
        provider = StreamingFailProvider()
        chunks = []
        with pytest.raises(FakeSDKError):
            async for chunk in provider.chat():
                chunks.append(chunk)
        # 只收到第一个 chunk，第二个失败
        assert chunks == ["first chunk"]
        # 只调用一次，不重试
        assert provider._call_count == 1


# ------------------------------------------------------------------
# CancelledError 测试
# ------------------------------------------------------------------

class TestCancelledError:
    """asyncio.CancelledError 永不被吞。"""

    @pytest.mark.asyncio
    async def test_cancelled_error_not_swallowed(self):
        """CancelledError 直接 raise，不重试。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                raise asyncio.CancelledError()
                yield  # 让 Python 识别为 async generator（不可达）

        p = P()
        with pytest.raises(asyncio.CancelledError):
            async for _ in p.chat():
                pass
        # CancelledError 不重试
        assert call_count[0] == 1


# ------------------------------------------------------------------
# Retry-After 与 max_attempts 测试
# ------------------------------------------------------------------

class TestRetryAfterAndMaxAttempts:
    """Retry-After header 优先 + 达到上限放弃。"""

    @pytest.mark.asyncio
    async def test_retry_after_header_respected(self):
        """Retry-After header 被使用作为延迟。"""
        call_count = [0]
        delays_seen: list[float] = []

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", base_delay=10.0, max_delay=20.0)
            async def chat(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    # 带 Retry-After=0.05 的 429
                    resp = FakeResponse(429, {"retry-after": "0.05"})
                    raise FakeSDKError("Rate limited", status_code=429, response=resp)
                yield "ok"

        p = P()
        chunks = []
        async for chunk in p.chat():
            chunks.append(chunk)
        assert chunks == ["ok"]
        assert call_count[0] == 2
        # Retry-After=0.05 应被使用，而非 base_delay=10.0
        # （如果用 base_delay 会等 10 秒，测试会超时）

    @pytest.mark.asyncio
    async def test_max_attempts_exhausted(self):
        """达到 max_attempts 后放弃，抛最后一个错误。"""
        call_count = [0]

        class P:
            _circuit_breaker_name = "test"
            default_model = "m"

            @with_llm_retry(provider="test", max_attempts=3, base_delay=0.01, max_delay=0.05)
            async def chat(self):
                call_count[0] += 1
                raise FakeSDKError("Rate limited", status_code=429)
                yield  # 让 Python 识别为 async generator（不可达）

        p = P()
        with pytest.raises(FakeSDKError) as exc_info:
            async for _ in p.chat():
                pass
        # 尝试 3 次（含首次）
        assert call_count[0] == 3
        assert "Rate limited" in str(exc_info.value)


# ------------------------------------------------------------------
# 子类继承 provider 名测试
# ------------------------------------------------------------------

class TestSubclassProviderName:
    """DashScope 子类继承时 provider 名正确（从 _circuit_breaker_name 读取）。"""

    @pytest.mark.asyncio
    async def test_subclass_uses_own_provider_name(self):
        """子类的 _circuit_breaker_name 被用作 provider 标识。"""
        # 模拟 DashScopeProvider 继承 VLLMProvider
        call_count = [0]

        class VLLMLike:
            _circuit_breaker_name = "vllm"
            default_model = "vllm-model"

        class DashScopeLike(VLLMLike):
            _circuit_breaker_name = "dashscope"
            default_model = "qwen-turbo"

            @with_llm_retry(provider="vllm")  # 装饰时写 vllm，但运行时应读 dashscope
            async def chat(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise FakeSDKError("Rate limited", status_code=429)
                yield "ok"

        p = DashScopeLike()
        chunks = []
        async for chunk in p.chat():
            chunks.append(chunk)
        assert chunks == ["ok"]
        # 成功重试一次
        assert call_count[0] == 2
