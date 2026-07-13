"""
LLM Provider 抽象基类 — 单一职责：定义统一的 LLM 调用接口与消息/工具类型。

遵循开闭原则：新增 Provider 只需继承 LLMProvider 并实现 chat，无需修改本文件。
遵循依赖倒置：业务层（RAG 引擎、Agent Loop）依赖 LLMProvider 抽象，
             不感知底层是 Anthropic API 还是本地 vLLM。

调用约定：
    chat 为异步生成器，调用方通过 ``async for chunk in provider.chat(...)`` 消费，
    无需 await（async generator 的标准用法）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal, TypedDict


class Message(TypedDict):
    """统一消息格式 — 所有 Provider 共用，屏蔽底层 SDK 差异。

    Attributes:
        role: 消息角色，取值 system / user / assistant。
        content: 消息文本内容。
    """

    role: Literal["system", "user", "assistant"]
    content: str


class Tool(TypedDict):
    """统一工具定义 — 各 Provider 负责转换为本厂 SDK 格式。

    Attributes:
        name: 工具名称（对应 MCP function name）。
        description: 工具描述，供 LLM 决策调用。
        parameters: 工具入参 JSON Schema。
    """

    name: str
    description: str
    parameters: dict[str, Any]


class ToolUse(TypedDict):
    """工具调用结果 — chat 流中以 dict 形态 yield 出来，供 Agent Loop 执行。

    Attributes:
        type: 固定为 "tool_use"，用于与文本片段（str）区分。
        id: 工具调用 ID，用于关联后续 tool_result。
        name: 要调用的工具名。
        input: 工具入参，已解析为 dict。
    """

    type: str
    id: str
    name: str
    input: dict[str, Any]


class LLMProvider(ABC):
    """LLM 统一接口 — 三种实现：SaaS(Anthropic) / 私有海外(vLLM-Llama) / 私有国内(vLLM-Qwen)。

    所有业务代码通过本抽象调用 LLM，具体实现由 ``app.llm.factory.get_llm_provider``
    按 ``DEPLOY_MODE`` 注入，实现"环境变量切换，业务代码零改动"。
    """

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        """与 LLM 对话，异步迭代返回文本片段或工具调用。

        Args:
            messages: 统一消息列表，role 为 system/user/assistant。
            tools: 可选工具列表（MCP function calling）。
            stream: 是否流式输出；True 时逐片段 yield，False 时一次性 yield 完整结果。
            **kwargs: 透传给底层 SDK 的生成参数，如 model / max_tokens / temperature 等。

        Yields:
            str: 文本片段（流式）或完整文本（非流式）。
            dict: 工具调用，形态见 ToolUse（type="tool_use"）。
        """
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本向量 — 可选实现。

        LLMProvider 默认不承担向量化职责；需要向量时请使用
        ``app.llm.embedder.get_embedder()`` 获取专用 EmbeddingProvider。
        子类若自身支持向量能力，可覆盖本方法。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 embed 方法，"
            "请使用 get_embedder() 获取专用向量服务。"
        )
