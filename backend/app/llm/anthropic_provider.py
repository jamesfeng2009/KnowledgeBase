"""
Anthropic LLM Provider — 单一职责：封装 Anthropic Claude API 调用。

支持 Claude Sonnet 4.6 / Opus 4.8，流式输出（messages.stream）与
tool_use（MCP function calling）。

P0-Opt1: 启用 Anthropic Prompt Caching — system prompt 标记 cache_control，
使稳定前缀以 0.1x 费率读取（而非 1x 全价）。首次写入 1.25x，5 分钟 TTL。
配合 CacheAligner 检测易变内容，防止缓存失效。

遵循开闭原则：新增模型只需在 ``ANTHROPIC_MODELS`` 注册表追加别名，无需改动 chat 逻辑。
遵循单一职责：本模块只负责 Anthropic 协议适配，不感知 DEPLOY_MODE 切换。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.config import get_settings
from app.llm.base import LLMProvider, Message, Tool, ToolUse
from app.llm.cache_aligner import check_cache_alignment
from app.utils.circuit_breaker import get_circuit_breaker
from app.utils.llm_retry import with_llm_retry
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()

# 模型别名注册表 — 开闭原则：新增模型在此追加即可，chat 逻辑无需修改。
ANTHROPIC_MODELS: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6-20260217",
    "opus": "claude-opus-4-8-20260217",
}

# Anthropic SDK 接受的透传生成参数白名单。
_PASSTHROUGH_PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
)


class AnthropicProvider(LLMProvider):
    """Anthropic Claude Provider — SaaS 模式主力 LLM。"""

    _circuit_breaker_name: str = "anthropic"

    def __init__(self, model: str | None = None) -> None:
        """初始化 Anthropic 异步客户端。

        Args:
            model: 默认模型，可传别名（"sonnet"/"opus"）或完整模型 ID；
                   为 None 时默认使用 Claude Sonnet 4.6。
        """
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.default_model = self._resolve_model(model or "sonnet")
        self._cb = get_circuit_breaker(self._circuit_breaker_name)

    @staticmethod
    def _resolve_model(model: str) -> str:
        """模型别名解析 — 别名查表，完整 ID 原样返回。"""
        return ANTHROPIC_MODELS.get(model, model)

    @staticmethod
    def _convert_tool(tool: Tool) -> dict[str, Any]:
        """统一 Tool → Anthropic tools 协议（input_schema）。"""
        return {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }

    @staticmethod
    def _split_system(messages: list[Message]) -> tuple[str, list[Message]]:
        """分离 system 消息 — Anthropic 要求 system 作为顶层参数，不混入 messages。"""
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        non_system = [m for m in messages if m["role"] != "system"]
        system_text = "\n\n".join(part for part in system_parts if part).strip()
        return system_text, non_system

    def _build_api_kwargs(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """构造 Anthropic API 调用参数。

        P0-Opt1: 启用 Prompt Caching — system prompt 包装为 content block 并标记
        cache_control: {"type": "ephemeral"}，使稳定前缀命中 Anthropic KV Cache。
        首次写入 1.25x 费率，后续 5 分钟内读取 0.1x 费率，显著降低重复前缀成本。
        """
        model = self._resolve_model(kwargs.pop("model", None) or self.default_model)
        max_tokens = kwargs.pop("max_tokens", 4096)
        system_text, non_system = self._split_system(messages)

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": non_system,
            "max_tokens": max_tokens,
        }

        # P0-Opt1: System prompt 标记 cache_control，启用 Anthropic Prompt Caching
        if system_text:
            # 检测易变内容（UUID/时间戳/JWT/哈希），防止缓存失效
            warnings = check_cache_alignment(system_text)
            for w in warnings:
                log.warning("llm.cache_aligner.volatile_content", warning=w)

            # 包装为 content block 并标记缓存点
            # Anthropic SDK 接受 system 为字符串或 content block 列表
            api_kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        if tools:
            api_kwargs["tools"] = [self._convert_tool(t) for t in tools]
        for key in _PASSTHROUGH_PARAMS:
            if key in kwargs:
                api_kwargs[key] = kwargs[key]
        return api_kwargs

    @with_llm_retry(provider="anthropic")
    async def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        """调用 Claude，流式/非流式均以异步生成器 yield 结果。

        流式模式：先逐片段 yield 文本，流结束后再 yield 完整 tool_use 块
        （tool_use 的 input 需由 SDK 装配完毕，故延后到流结束统一输出）。

        熔断保护：调用前检查熔断器状态，调用后记录成功/失败。
        流式途中客户端断开（GeneratorExit / CancelledError）时，在 finally
        中释放 half-open 探测许可，避免熔断器永久卡 HALF_OPEN。
        """
        from app.utils.circuit_breaker import CircuitBreakerOpenError, CircuitState

        # 熔断器检查 — OPEN 状态快速失败
        if self._cb.state == CircuitState.OPEN:
            if self._cb._should_transition_to_half_open():
                self._cb.state = CircuitState.HALF_OPEN
                self._cb.half_open_calls = 0
                log.info("circuit_breaker.transition", name=self._cb.name,
                         from_state="open", to_state="half_open")
            else:
                log.warning("circuit_breaker.rejected", name=self._cb.name, state="open")
                raise CircuitBreakerOpenError(self._cb.name, self._cb.state)

        # half-open 探测许可 — 获取后必须在 finally 中记录结果或释放，
        # 否则流式中断会泄漏许可，half_open_calls 达到上限后永久拒绝请求。
        half_open_probe = False
        if self._cb.state == CircuitState.HALF_OPEN:
            if self._cb.half_open_calls >= self._cb.half_open_max_calls:
                log.warning("circuit_breaker.rejected", name=self._cb.name, state="half_open")
                raise CircuitBreakerOpenError(self._cb.name, self._cb.state)
            self._cb.half_open_calls += 1
            half_open_probe = True

        import time
        t0 = time.monotonic()
        # 调用结果是否已记录到熔断器（成功/失败均置 True）
        outcome_recorded = False

        try:
            api_kwargs = self._build_api_kwargs(messages, tools, kwargs)
            log.info("llm.anthropic.chat.start", model=api_kwargs.get("model"),
                     msg_count=len(messages), stream=stream, has_tools=bool(tools))

            if stream:
                async with self.client.messages.stream(**api_kwargs) as stream_resp:
                    async for text in stream_resp.text_stream:
                        if text:
                            yield text
                    # 流结束后取完整消息，统一输出 tool_use 块
                    final_message = await stream_resp.get_final_message()
                    for block in final_message.content:
                        if block.type == "tool_use":
                            yield ToolUse(
                                type="tool_use",
                                id=block.id,
                                name=block.name,
                                input=dict(block.input) if block.input else {},
                            )
                    # P0-Stage2: 提取真实 token 用量供 UsageRecord 记录
                    _usage = getattr(final_message, "usage", None)
                    if _usage:
                        yield {
                            "type": "usage",
                            "input_tokens": getattr(_usage, "input_tokens", 0) or 0,
                            "output_tokens": getattr(_usage, "output_tokens", 0) or 0,
                            "model": api_kwargs.get("model", ""),
                        }
            else:
                resp = await self.client.messages.create(**api_kwargs)
                for block in resp.content:
                    if block.type == "text":
                        yield block.text
                    elif block.type == "tool_use":
                        yield ToolUse(
                            type="tool_use",
                            id=block.id,
                            name=block.name,
                            input=dict(block.input) if block.input else {},
                        )
                # P0-Stage2: 提取真实 token 用量供 UsageRecord 记录
                _usage = getattr(resp, "usage", None)
                if _usage:
                    yield {
                        "type": "usage",
                        "input_tokens": getattr(_usage, "input_tokens", 0) or 0,
                        "output_tokens": getattr(_usage, "output_tokens", 0) or 0,
                        "model": api_kwargs.get("model", ""),
                    }

            # 调用成功 — 记录到熔断器
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.info("llm.anthropic.chat.success", latency_ms=elapsed_ms)
            self._cb._record_success()
            outcome_recorded = True

        except CircuitBreakerOpenError:
            raise
        except Exception as exc:
            # 调用失败 — 记录到熔断器
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.warning("llm.anthropic.chat.error", error=str(exc), latency_ms=elapsed_ms)
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
