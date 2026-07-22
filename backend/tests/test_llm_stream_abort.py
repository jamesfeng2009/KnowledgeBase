"""LLM Provider 流式中断（GeneratorExit）熔断器回归测试。

Bug 背景：流式途中客户端断开（生成器被 aclose → GeneratorExit 抛入 yield
点）时，GeneratorExit 继承 BaseException，不被 ``except Exception`` 捕获，
成功/失败均未记录 — HALF_OPEN 探测许可（half_open_calls += 1）永久泄漏，
达到 half_open_max_calls 上限后熔断器永远拒绝后续请求（卡死 HALF_OPEN）。

修复：chat() 在 finally 中检查 — 若本次调用持有 half-open 探测许可且结果
未被记录（既非成功也非失败），则释放许可（half_open_calls -= 1）。

覆盖（VLLM / Anthropic / DashScope 三个 Provider）：
- HALF_OPEN 下流式中断 → 许可释放，后续探测可正常进入并恢复 CLOSED；
- CLOSED 下流式中断 → 不计失败、状态不变（客户端断开 ≠ 下游故障）；
- HALF_OPEN 下流式途中异常 → 仍按失败记录 → 重新 OPEN（既有行为保持）；
- HALF_OPEN 下完整消费 → 记录成功 → CLOSED（既有行为保持）。
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 设置测试用 dummy API key — 避免构造 Provider 时报错
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")

from app.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitState,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


@pytest.fixture(autouse=True)
def reset_breakers():
    """每个测试前后重置所有熔断器，保证隔离。"""
    reset_all_circuit_breakers()
    yield
    reset_all_circuit_breakers()


# ---------------------------------------------------------------------------
# Mock 流式响应
# ---------------------------------------------------------------------------


def _make_text_chunk(content: str = "hello"):
    """构造 OpenAI 兼容的流式 chunk。"""
    chunk = MagicMock()
    chunk.choices = [MagicMock(delta=MagicMock(content=content, tool_calls=None))]
    return chunk


class _HangingStream:
    """模拟 OpenAI 兼容流式响应：产出首 chunk 后永久挂起（客户端断连场景）。"""

    def __init__(self, first_chunk):
        self._first = first_chunk
        self._delivered = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._delivered:
            self._delivered = True
            return self._first
        await asyncio.Event().wait()  # 永久挂起直到任务被取消
        raise StopAsyncIteration  # pragma: no cover — 不会到达


class _FiniteStream:
    """产出固定 chunk 列表后正常结束的流。"""

    def __init__(self, chunks: list):
        self._chunks = chunks
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _FailingStream:
    """产出首 chunk 后抛错的流（下游中途故障场景）。"""

    def __init__(self, first_chunk):
        self._first = first_chunk
        self._delivered = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._delivered:
            self._delivered = True
            return self._first
        raise RuntimeError("stream broken mid-flight")


class _HangingTextStream:
    """Anthropic text_stream 版本：产出首个文本后永久挂起。"""

    def __init__(self, first_text: str):
        self._first = first_text
        self._delivered = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._delivered:
            self._delivered = True
            return self._first
        await asyncio.Event().wait()
        raise StopAsyncIteration  # pragma: no cover — 不会到达


class _AnthropicStreamCtx:
    """模拟 Anthropic messages.stream() 返回的异步上下文管理器。"""

    def __init__(self, text_stream):
        self.text_stream = text_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        msg = MagicMock()
        msg.content = []
        return msg


def _enter_half_open(name: str) -> None:
    """将指定熔断器置于 HALF_OPEN 且许可未占用（等价于 OPEN 冷却结束转入）。"""
    cb = get_circuit_breaker(name)
    cb.state = CircuitState.HALF_OPEN
    cb.half_open_calls = 0
    cb.half_open_max_calls = 1


# ---------------------------------------------------------------------------
# VLLMProvider
# ---------------------------------------------------------------------------


class TestVLLMStreamAbort:
    """VLLMProvider 流式中断的熔断器许可释放。"""

    def _make_provider(self):
        from app.llm.vllm_provider import VLLMProvider

        return VLLMProvider()

    def test_generator_exit_releases_half_open_permit(self):
        """HALF_OPEN 探测途中客户端断开 → 许可释放，后续探测可恢复 CLOSED。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("vllm")
        _enter_half_open("vllm")

        provider.client.chat.completions.create = AsyncMock(
            return_value=_HangingStream(_make_text_chunk())
        )

        async def consume_and_abort():
            gen = provider.chat([{"role": "user", "content": "hi"}], stream=True)
            first = await gen.__anext__()
            assert first == "hello"
            assert cb.half_open_calls == 1  # 探测许可已占用
            # 模拟客户端断连 — GeneratorExit 抛入 yield 点
            await gen.aclose()

        asyncio.run(consume_and_abort())

        # 许可已释放：状态仍 HALF_OPEN，计数归零（修复前：卡在 1，永久拒绝）
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.half_open_calls == 0

        # 后续探测可正常进入并成功 → 熔断器关闭
        provider.client.chat.completions.create = AsyncMock(
            return_value=_FiniteStream([_make_text_chunk("ok")])
        )

        async def consume():
            return [t async for t in provider.chat([{"role": "user", "content": "hi"}], stream=True)]

        tokens = asyncio.run(consume())
        assert "ok" in tokens
        assert cb.state == CircuitState.CLOSED

    def test_abort_in_closed_state_records_nothing(self):
        """CLOSED 下客户端断开 → 不计成功也不计失败，状态保持 CLOSED。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("vllm")

        provider.client.chat.completions.create = AsyncMock(
            return_value=_HangingStream(_make_text_chunk())
        )

        async def consume_and_abort():
            gen = provider.chat([{"role": "user", "content": "hi"}], stream=True)
            await gen.__anext__()
            await gen.aclose()

        asyncio.run(consume_and_abort())

        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_stream_failure_in_half_open_reopens_circuit(self):
        """HALF_OPEN 探测途中下游真的故障 → 记录失败 → 重新 OPEN（行为保持）。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("vllm")
        _enter_half_open("vllm")

        provider.client.chat.completions.create = AsyncMock(
            return_value=_FailingStream(_make_text_chunk())
        )

        async def consume():
            async for _ in provider.chat([{"role": "user", "content": "hi"}], stream=True):
                pass

        with pytest.raises(RuntimeError, match="stream broken"):
            asyncio.run(consume())

        assert cb.state == CircuitState.OPEN

    def test_full_consume_in_half_open_closes_circuit(self):
        """HALF_OPEN 探测完整消费 → 记录成功 → CLOSED（行为保持）。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("vllm")
        _enter_half_open("vllm")

        provider.client.chat.completions.create = AsyncMock(
            return_value=_FiniteStream([_make_text_chunk("fine")])
        )

        async def consume():
            return [t async for t in provider.chat([{"role": "user", "content": "hi"}], stream=True)]

        tokens = asyncio.run(consume())
        assert "fine" in tokens
        assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


class TestAnthropicStreamAbort:
    """AnthropicProvider 流式中断的熔断器许可释放。"""

    def _make_provider(self):
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider()

    def test_generator_exit_releases_half_open_permit(self):
        """HALF_OPEN 探测途中客户端断开 → 许可释放，后续探测可恢复 CLOSED。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("anthropic")
        _enter_half_open("anthropic")

        provider.client.messages.stream = MagicMock(
            return_value=_AnthropicStreamCtx(_HangingTextStream("hello"))
        )

        async def consume_and_abort():
            gen = provider.chat([{"role": "user", "content": "hi"}], stream=True)
            first = await gen.__anext__()
            assert first == "hello"
            assert cb.half_open_calls == 1  # 探测许可已占用
            await gen.aclose()

        asyncio.run(consume_and_abort())

        # 许可已释放（修复前：卡在 1，后续请求被 CircuitBreakerOpenError 拒绝）
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.half_open_calls == 0

        # 后续探测可正常进入并成功 → 熔断器关闭
        provider.client.messages.stream = MagicMock(
            return_value=_AnthropicStreamCtx(_FiniteStream(["ok"]))
        )

        async def consume():
            return [t async for t in provider.chat([{"role": "user", "content": "hi"}], stream=True)]

        tokens = asyncio.run(consume())
        assert "ok" in tokens
        assert cb.state == CircuitState.CLOSED

    def test_abort_in_closed_state_records_nothing(self):
        """CLOSED 下客户端断开 → 不计成功也不计失败。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("anthropic")

        provider.client.messages.stream = MagicMock(
            return_value=_AnthropicStreamCtx(_HangingTextStream("hi"))
        )

        async def consume_and_abort():
            gen = provider.chat([{"role": "user", "content": "hi"}], stream=True)
            await gen.__anext__()
            await gen.aclose()

        asyncio.run(consume_and_abort())

        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0


# ---------------------------------------------------------------------------
# DashScopeProvider（chat 逻辑继承自 VLLMProvider — 验证继承路径同样修复）
# ---------------------------------------------------------------------------


class TestDashScopeStreamAbort:
    """DashScopeProvider 流式中断的熔断器许可释放（继承 VLLMProvider.chat）。"""

    def _make_provider(self):
        with patch("app.llm.dashscope_provider.settings") as mock_settings:
            mock_settings.DASHSCOPE_BASE_URL = (
                "https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
            mock_settings.DASHSCOPE_API_KEY = "sk-test"
            mock_settings.DASHSCOPE_LLM_MODEL = "qwen-turbo"
            from app.llm.dashscope_provider import DashScopeProvider

            return DashScopeProvider()

    def test_generator_exit_releases_half_open_permit(self):
        """HALF_OPEN 探测途中客户端断开 → 许可释放，后续探测可恢复 CLOSED。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("dashscope")
        _enter_half_open("dashscope")

        provider.client.chat.completions.create = AsyncMock(
            return_value=_HangingStream(_make_text_chunk())
        )

        async def consume_and_abort():
            gen = provider.chat([{"role": "user", "content": "hi"}], stream=True)
            first = await gen.__anext__()
            assert first == "hello"
            assert cb.half_open_calls == 1
            await gen.aclose()

        asyncio.run(consume_and_abort())

        assert cb.state == CircuitState.HALF_OPEN
        assert cb.half_open_calls == 0

        provider.client.chat.completions.create = AsyncMock(
            return_value=_FiniteStream([_make_text_chunk("ok")])
        )

        async def consume():
            return [t async for t in provider.chat([{"role": "user", "content": "hi"}], stream=True)]

        tokens = asyncio.run(consume())
        assert "ok" in tokens
        assert cb.state == CircuitState.CLOSED

    def test_released_permit_allows_new_probe_immediately(self):
        """许可释放后，新的探测不会被 CircuitBreakerOpenError 拒绝（幂等释放）。"""
        provider = self._make_provider()
        cb = get_circuit_breaker("dashscope")
        _enter_half_open("dashscope")

        # side_effect 工厂 — 每次调用返回全新挂起流（避免跨调用复用同一实例）
        provider.client.chat.completions.create = AsyncMock(
            side_effect=lambda **kw: _HangingStream(_make_text_chunk())
        )

        async def consume_and_abort():
            gen = provider.chat([{"role": "user", "content": "hi"}], stream=True)
            await gen.__anext__()
            await gen.aclose()

        # 连续两次中断 — 每次释放都应幂等（计数不为负、状态不漂移）
        asyncio.run(consume_and_abort())
        asyncio.run(consume_and_abort())

        assert cb.state == CircuitState.HALF_OPEN
        assert cb.half_open_calls == 0

        # 新探测正常放行（修复前此处抛 CircuitBreakerOpenError）
        provider.client.chat.completions.create = AsyncMock(
            return_value=_FiniteStream([_make_text_chunk("ok")])
        )

        async def consume():
            async for _ in provider.chat([{"role": "user", "content": "hi"}], stream=True):
                pass

        asyncio.run(consume())
        assert cb.state == CircuitState.CLOSED
