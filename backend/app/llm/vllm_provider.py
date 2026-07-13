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

settings = get_settings()

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
        """
        api_kwargs = self._build_api_kwargs(messages, tools, kwargs)

        if stream:
            api_kwargs["stream"] = True
            response = await self.client.chat.completions.create(**api_kwargs)

            # tool_calls 在流中以增量分片到达，需按 index 缓冲装配。
            tool_buffers: dict[int, dict[str, Any]] = {}
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    yield delta.content
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index if tc.index is not None else len(tool_buffers)
                        buf = tool_buffers.setdefault(
                            idx,
                            {"type": "tool_use", "id": "", "name": "", "input": ""},
                        )
                        if tc.id:
                            buf["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                buf["name"] = tc.function.name
                            if tc.function.arguments:
                                buf["input"] += tc.function.arguments

            # 流结束：解析装配好的 tool_calls，按 index 顺序输出。
            for idx in sorted(tool_buffers):
                buf = tool_buffers[idx]
                buf["input"] = _parse_json_object(buf["input"])
                yield buf

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


def _parse_json_object(raw: str) -> dict[str, Any]:
    """安全解析 tool_call 入参 JSON，失败时回退为空 dict。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
