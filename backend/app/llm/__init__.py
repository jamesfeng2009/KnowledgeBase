"""
LLM Provider 抽象层 — 统一 LLM / Embedding 调用接口。

对外暴露工厂函数 ``get_llm_provider`` / ``get_embedder`` 及统一类型，
业务层仅依赖本包导出的抽象，按 DEPLOY_MODE 切换底层实现，业务代码零改动。

典型用法::

    from app.llm import get_llm_provider, Message

    provider = get_llm_provider()
    messages: list[Message] = [
        {"role": "system", "content": "你是一名知识库助手。"},
        {"role": "user", "content": "RAG 的权限过滤顺序是什么？"},
    ]
    async for chunk in provider.chat(messages, stream=True):
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

from app.llm.base import LLMProvider, Message, Tool, ToolUse
from app.llm.embedder import EmbeddingProvider
from app.llm.factory import get_embedder, get_llm_provider, list_llm_providers

__all__ = [
    "get_llm_provider",
    "get_embedder",
    "list_llm_providers",
    "LLMProvider",
    "EmbeddingProvider",
    "Message",
    "Tool",
    "ToolUse",
]
