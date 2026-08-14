"""
vLLM LLM Provider — 单一职责：通过 OpenAI SDK 调用本地 vLLM 服务。

vLLM 启动后暴露 OpenAI 兼容 API，因此复用 openai.AsyncOpenAI 客户端。
覆盖私有部署·海外（Llama 3.3 70B）与私有部署·国内（Qwen 3 72B）两种场景。

遵循开闭原则：新增本地模型只需在 factory 传入不同 model，无需子类化、无需改本文件。
遵循单一职责：本模块只负责 vLLM(OpenAI 兼容) 协议适配。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings
from app.llm.base import LLMProvider, Message, Tool, ToolUse
from app.utils.circuit_breaker import get_circuit_breaker
from app.utils.llm_retry import with_llm_retry
from app.utils.logger import get_logger

settings = get_settings()
log = get_logger(__name__)

# OpenAI 兼容 API 接受的透传生成参数白名单。
_PASSTHROUGH_PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "stop",
    "presence_penalty",
    "frequency_penalty",
)


class VLLMProvider(LLMProvider):
    """本地 vLLM Provider — 通过 OpenAI 兼容 API 调用自托管模型。"""

    # 熔断器名称 — 子类可覆盖（如 DashScopeProvider 用 "dashscope"）
    _circuit_breaker_name: str = "vllm"

    def __init__(self, model: str | None = None) -> None:
        """初始化 vLLM 异步客户端。

        Args:
            model: 默认模型 ID（如 meta-llama/Llama-3.3-70B-Instruct）；
                   为 None 时回退到 settings.VLLM_MODEL。
        """
        # vLLM 本地部署无需真实鉴权，api_key 占位即可。
        self.client = AsyncOpenAI(
            base_url=settings.vllm_base_url,
            api_key="dummy",
        )
        self.default_model = model or settings.VLLM_MODEL
        self._cb = get_circuit_breaker(self._circuit_breaker_name)

    @staticmethod
    def _convert_tool(tool: Tool) -> dict[str, Any]:
        """统一 Tool → OpenAI function calling 协议。"""
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }

    def _build_api_kwargs(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """构造 OpenAI 兼容 API 调用参数。"""
        model = kwargs.pop("model", None) or self.default_model
        max_tokens = kwargs.pop("max_tokens", 4096)

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            api_kwargs["tools"] = [self._convert_tool(t) for t in tools]
        for key in _PASSTHROUGH_PARAMS:
            if key in kwargs:
                api_kwargs[key] = kwargs[key]
        return api_kwargs

    @with_llm_retry(provider="vllm")
    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        """调用 vLLM，流式/非流式均以异步生成器 yield 结果。

        流式模式：文本片段实时 yield；tool_calls 分片跨 chunk 装配，
        流结束后按 index 顺序统一 yield 完整 ToolUse。

        熔断保护：调用前检查熔断器状态，调用后记录成功/失败。
        熔断开启时抛出 CircuitBreakerOpenError，不执行实际调用。
        流式途中客户端断开（GeneratorExit / CancelledError）时，在 finally
        中释放 half-open 探测许可，避免熔断器永久卡 HALF_OPEN。
        """
        from app.utils.circuit_breaker import CircuitBreakerOpenError, CircuitState

        # 熔断器检查 — 使用 circuit_breaker 原子操作，避免 half_open_calls
        # 检查与递增之间的竞态，防止高并发下半开探测数超过配置上限。
        self._cb._check_and_enter()
        half_open_probe = self._cb.state == CircuitState.HALF_OPEN

        import time
        t0 = time.monotonic()
        # 调用结果是否已记录到熔断器（成功/失败均置 True）
        outcome_recorded = False

        try:
            api_kwargs = self._build_api_kwargs(messages, tools, kwargs)
            log.info("llm.chat.start", provider=self._circuit_breaker_name,
                     model=api_kwargs.get("model"),
                     msg_count=len(messages), stream=stream, has_tools=bool(tools))

            if stream:
                api_kwargs["stream"] = True
                # P0-Stage2: 请求流式 usage（最后一个 chunk 携带汇总用量）
                api_kwargs["stream_options"] = {"include_usage": True}
                response = await self.client.chat.completions.create(**api_kwargs)

                # tool_calls 在流中以增量分片到达，需按 tool_call id 缓冲装配。
                # 优先用 tc.id 聚合；无 id 时回退到 index；均无则沿用上一个 tool key，
                # 避免 index=None 时使用 len(tool_buffers) 导致同一 tool_call 被拆碎。
                tool_buffers: dict[str, dict[str, Any]] = {}
                tool_order: list[str] = []
                last_tool_key: str | None = None
                _stream_usage = None
                async for chunk in response:
                    # 最后一个 chunk 可能 choices 为空但携带 usage 汇总
                    if chunk.usage:
                        _stream_usage = chunk.usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        yield delta.content
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            if tc.id:
                                key = tc.id
                            elif tc.index is not None:
                                key = str(tc.index)
                            elif last_tool_key is not None:
                                key = last_tool_key
                            else:
                                key = f"__anon_{len(tool_buffers)}"

                            if key not in tool_buffers:
                                tool_buffers[key] = {
                                    "type": "tool_use",
                                    "id": tc.id or "",
                                    "name": "",
                                    "input": "",
                                }
                                tool_order.append(key)

                            buf = tool_buffers[key]
                            if tc.id:
                                buf["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    buf["name"] = tc.function.name
                                if tc.function.arguments:
                                    buf["input"] += tc.function.arguments
                            last_tool_key = key

                # 流结束：解析装配好的 tool_calls，按出现顺序输出。
                for key in tool_order:
                    buf = tool_buffers[key]
                    buf["input"] = _parse_json_object(buf["input"])
                    yield buf

                # P0-Stage2: 提取真实 token 用量供 UsageRecord 记录
                if _stream_usage:
                    yield {
                        "type": "usage",
                        "input_tokens": getattr(_stream_usage, "prompt_tokens", 0) or 0,
                        "output_tokens": getattr(_stream_usage, "completion_tokens", 0) or 0,
                        "model": api_kwargs.get("model", ""),
                    }

            else:
                resp = await self.client.chat.completions.create(**api_kwargs)
                message = resp.choices[0].message
                if message.content:
                    yield message.content
                if message.tool_calls:
                    for tc in message.tool_calls:
                        arguments = "{}"
                        if tc.function and tc.function.arguments:
                            arguments = tc.function.arguments
                        yield ToolUse(
                            type="tool_use",
                            id=tc.id or "",
                            name=tc.function.name if tc.function else "",
                            input=_parse_json_object(arguments),
                        )
                # P0-Stage2: 提取真实 token 用量供 UsageRecord 记录
                if resp.usage:
                    yield {
                        "type": "usage",
                        "input_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                        "output_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
                        "model": api_kwargs.get("model", ""),
                    }

            # 调用成功 — 记录到熔断器
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.info("llm.chat.success", provider=self._circuit_breaker_name, latency_ms=elapsed_ms)
            self._cb._record_success()
            outcome_recorded = True

        except CircuitBreakerOpenError:
            raise
        except Exception as exc:
            # 调用失败 — 记录到熔断器
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.warning("llm.chat.error", provider=self._circuit_breaker_name,
                        error=str(exc), latency_ms=elapsed_ms)
            self._cb._record_failure()
            outcome_recorded = True
            raise
        finally:
            # GeneratorExit（客户端断连）/ CancelledError（任务取消）路径：
            # 既非成功也非失败，不记录结果；但必须释放 half-open 探测许可，
            # 否则 half_open_calls 达到上限后熔断器永久卡 HALF_OPEN。
            if half_open_probe and not outcome_recorded:
                with self._cb._lock:
                    if (
                        self._cb.state == CircuitState.HALF_OPEN
                        and self._cb.half_open_calls > 0
                    ):
                        self._cb.half_open_calls -= 1
                        log.info(
                            "circuit_breaker.probe_released",
                            name=self._cb.name,
                            half_open_calls=self._cb.half_open_calls,
                            reason="stream_aborted",
                        )


def _parse_json_object(raw: str) -> dict[str, Any]:
    """安全解析 tool_call 入参 JSON，失败时回退为空 dict。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
