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

    def __init__(self, model: str | None = None) -> None:
        """初始化 Anthropic 异步客户端。

        Args:
            model: 默认模型，可传别名（"sonnet"/"opus"）或完整模型 ID；
                   为 None 时默认使用 Claude Sonnet 4.6。
        """
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.default_model = self._resolve_model(model or "sonnet")

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
        """
        api_kwargs = self._build_api_kwargs(messages, tools, kwargs)

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
