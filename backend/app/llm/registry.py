"""Provider 注册表 — 管理 AI 服务 Provider 的元数据和工厂函数。

P2-A Task 2: 为 HealthCheckService 提供 Provider 发现能力。
注册表使用延迟导入，未安装的依赖不会导致注册失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ProviderMeta:
    """Provider 元数据 — 描述一个可被健康检查的 Provider。"""

    name: str  # 逻辑名称 (e.g., "openai")
    type: str  # 类型: "embedder" | "reranker" | "vectorstore" | "llm"
    breaker_name: str  # 熔断器名称 (e.g., "embedder_openai")
    factory: Callable[[], Any]  # 工厂函数 — 调用返回 Provider 实例


def get_embedder_entries() -> list[ProviderMeta]:
    """返回所有可用的 Embedder 注册项。"""
    entries: list[ProviderMeta] = []
    try:
        from app.llm.embedder import OpenAIEmbedder

        entries.append(
            ProviderMeta("openai", "embedder", "embedder_openai", OpenAIEmbedder)
        )
    except Exception as exc:
        log.debug("registry.embedder.skip", name="openai", error=str(exc))

    try:
        from app.llm.embedder import TEIEmbedder

        entries.append(
            ProviderMeta("tei", "embedder", "embedder_tei", TEIEmbedder)
        )
    except Exception as exc:
        log.debug("registry.embedder.skip", name="tei", error=str(exc))

    try:
        from app.llm.embedder import DashScopeEmbedder

        entries.append(
            ProviderMeta(
                "dashscope", "embedder", "embedder_dashscope", DashScopeEmbedder
            )
        )
    except Exception as exc:
        log.debug("registry.embedder.skip", name="dashscope", error=str(exc))

    return entries


def get_reranker_entries() -> list[ProviderMeta]:
    """返回所有可用的 Reranker 注册项。"""
    entries: list[ProviderMeta] = []
    try:
        from app.rag.reranker import CohereReranker

        entries.append(
            ProviderMeta("cohere", "reranker", "reranker_cohere", CohereReranker)
        )
    except Exception as exc:
        log.debug("registry.reranker.skip", name="cohere", error=str(exc))

    try:
        from app.rag.reranker import TEIReranker

        entries.append(
            ProviderMeta("tei", "reranker", "reranker_tei", TEIReranker)
        )
    except Exception as exc:
        log.debug("registry.reranker.skip", name="tei", error=str(exc))

    return entries


def get_vector_store_entries() -> list[ProviderMeta]:
    """返回所有可用的 VectorStore 注册项。"""
    entries: list[ProviderMeta] = []
    try:
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        entries.append(
            ProviderMeta(
                "opensearch",
                "vectorstore",
                "vectorstore_opensearch",
                OpenSearchVectorStore,
            )
        )
    except Exception as exc:
        log.debug("registry.vectorstore.skip", name="opensearch", error=str(exc))

    try:
        from app.rag.vector_store.milvus_store import MilvusVectorStore

        entries.append(
            ProviderMeta(
                "milvus", "vectorstore", "vectorstore_milvus", MilvusVectorStore
            )
        )
    except Exception as exc:
        log.debug("registry.vectorstore.skip", name="milvus", error=str(exc))

    return entries


def get_llm_provider_entries() -> list[ProviderMeta]:
    """返回所有可用的 LLM Provider 注册项。"""
    entries: list[ProviderMeta] = []
    try:
        from app.llm.vllm_provider import VLLMProvider

        entries.append(
            ProviderMeta("vllm", "llm", "vllm", VLLMProvider)
        )
    except Exception as exc:
        log.debug("registry.llm.skip", name="vllm", error=str(exc))

    try:
        from app.llm.anthropic_provider import AnthropicProvider

        entries.append(
            ProviderMeta("anthropic", "llm", "anthropic", AnthropicProvider)
        )
    except Exception as exc:
        log.debug("registry.llm.skip", name="anthropic", error=str(exc))

    try:
        from app.llm.dashscope_provider import DashScopeProvider

        entries.append(
            ProviderMeta("dashscope", "llm", "dashscope", DashScopeProvider)
        )
    except Exception as exc:
        log.debug("registry.llm.skip", name="dashscope", error=str(exc))

    return entries


def get_all_provider_entries() -> list[ProviderMeta]:
    """返回所有可用的 Provider 注册项。"""
    return [
        *get_embedder_entries(),
        *get_reranker_entries(),
        *get_vector_store_entries(),
        *get_llm_provider_entries(),
    ]
