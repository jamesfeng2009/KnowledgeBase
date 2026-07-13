"""
Provider 工厂 — 单一职责：根据 DEPLOY_MODE 创建对应 LLM / Embedding Provider。

采用注册表（registry + decorator）模式实现开闭原则：
新增 Provider 只需新增一个 ``@register_llm_provider`` 工厂函数，
无需修改 ``get_llm_provider`` 内部分支逻辑，也不改动既有工厂函数。

部署模式映射：
    saas              → AnthropicProvider（Claude Sonnet 4.6）
    private_overseas  → VLLMProvider（Llama 3.3 70B）
    private_domestic  → VLLMProvider（Qwen 3 72B）

Embedder 复用 ``app.llm.embedder.get_embedder``，本模块统一对外暴露，
使调用方只需 ``from app.llm.factory import get_llm_provider, get_embedder``。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from app.config import get_settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.embedder import EmbeddingProvider, get_embedder
from app.llm.vllm_provider import VLLMProvider

settings = get_settings()

__all__ = [
    "get_llm_provider",
    "get_embedder",
    "list_llm_providers",
    "LLMProvider",
    "EmbeddingProvider",
]

# LLM Provider 工厂注册表 — deploy_mode → 工厂函数。
_llm_provider_registry: dict[str, Callable[[], LLMProvider]] = {}


def register_llm_provider(
    deploy_mode: str,
) -> Callable[[Callable[[], LLMProvider]], Callable[[], LLMProvider]]:
    """装饰器：注册某部署模式对应的 LLM Provider 工厂函数。

    开闭原则的落点：新增部署模式只追加一个新的被装饰函数，
    不触碰 ``get_llm_provider`` 与既有注册项。
    """

    def decorator(
        factory: Callable[[], LLMProvider],
    ) -> Callable[[], LLMProvider]:
        _llm_provider_registry[deploy_mode] = factory
        return factory

    return decorator


@register_llm_provider("saas")
def _make_anthropic_provider() -> AnthropicProvider:
    """SaaS 模式：Claude Sonnet 4.6。"""
    return AnthropicProvider()


@register_llm_provider("private_overseas")
def _make_vllm_llama_provider() -> VLLMProvider:
    """私有部署·海外：Llama 3.3 70B（Meta）。"""
    return VLLMProvider(model="meta-llama/Llama-3.3-70B-Instruct")


@register_llm_provider("private_domestic")
def _make_vllm_qwen_provider() -> VLLMProvider:
    """私有部署·国内：Qwen 3 72B（阿里）。"""
    return VLLMProvider(model="Qwen/Qwen3-72B-Instruct")


@lru_cache
def get_llm_provider() -> LLMProvider:
    """获取当前部署模式的 LLM Provider（单例，复用底层 SDK 连接）。

    Returns:
        对应 DEPLOY_MODE 的 LLMProvider 实例。

    Raises:
        ValueError: DEPLOY_MODE 未在注册表中。
    """
    mode = settings.DEPLOY_MODE
    factory = _llm_provider_registry.get(mode)
    if factory is None:
        raise ValueError(
            f"不支持的 DEPLOY_MODE: {mode}，"
            f"已注册: {list(_llm_provider_registry)}"
        )
    return factory()


def list_llm_providers() -> dict[str, str]:
    """调试/可观测用：返回已注册的 deploy_mode → 工厂名映射。"""
    return {mode: factory.__name__ for mode, factory in _llm_provider_registry.items()}
