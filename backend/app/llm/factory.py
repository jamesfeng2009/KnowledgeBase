"""
Provider 工厂 — 单一职责：根据 DEPLOY_MODE 创建对应 LLM / Embedding Provider。

采用注册表（registry + decorator）模式实现开闭原则：
新增 Provider 只需新增一个 ``@register_llm_provider`` 工厂函数，
无需修改 ``get_llm_provider`` 内部分支逻辑，也不改动既有工厂函数。

部署模式映射：
    saas              → AnthropicProvider（Claude Sonnet 4.6）
    saas_dashscope    → DashScopeProvider（通义千问 Qwen，国内 SaaS）
    private_overseas  → VLLMProvider（Llama 3.3 70B）
    private_domestic  → VLLMProvider（Qwen 3 72B）

P2-3 扩展：``get_llm_provider_by_model(model_id)`` 根据 models.json 中的
模型 ID 创建对应 Provider，支持用户在会话级切换模型。

Embedder 复用 ``app.llm.embedder.get_embedder``，本模块统一对外暴露，
使调用方只需 ``from app.llm.factory import get_llm_provider, get_embedder``。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from app.config import get_settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.dashscope_provider import DashScopeProvider
from app.llm.embedder import EmbeddingProvider, get_embedder
from app.llm.model_config import get_model_by_id
from app.llm.vllm_provider import VLLMProvider
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()

__all__ = [
    "get_llm_provider",
    "get_llm_provider_by_model",
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


@register_llm_provider("saas_dashscope")
def _make_dashscope_provider() -> DashScopeProvider:
    """SaaS·国内模式：通义千问 Qwen via DashScope。"""
    return DashScopeProvider()


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


# P2-3: 按 model_id 缓存的 Provider 实例 — 避免每次请求创建新 SDK 客户端
# key: model_id（如 "claude-haiku-4"），value: LLMProvider 实例
_model_provider_cache: dict[str, LLMProvider] = {}

# Provider 类型 → 构造函数映射
_PROVIDER_CONSTRUCTORS: dict[str, Callable[..., LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "dashscope": DashScopeProvider,
    "vllm": VLLMProvider,
}


def get_llm_provider_by_model(model_id: str) -> LLMProvider:
    """根据 model_id 创建/获取 LLM Provider（P2 用户级模型选择）。

    从 models.json 查找模型配置，根据 provider_type 创建对应 Provider，
    传入实际的 model_id（如 "claude-haiku-4-20250506"）。

    缓存策略：每个 model_id 只创建一次 Provider 实例（复用 SDK 连接）。
    与 ``get_llm_provider()`` 的区别：后者返回 DEPLOY_MODE 默认 Provider（单例），
    本函数返回指定模型的 Provider（按 model_id 缓存）。

    Args:
        model_id: models.json 中的模型 ID（如 "claude-haiku-4"），非 Provider 实际模型名。

    Returns:
        对应模型的 LLMProvider 实例。

    Raises:
        ValueError: model_id 不存在、不属于当前部署模式、或 provider_type 未注册。
    """
    # 1. 检查缓存
    if model_id in _model_provider_cache:
        return _model_provider_cache[model_id]

    # 2. 查找模型配置
    model_config = get_model_by_id(model_id)
    if model_config is None:
        raise ValueError(f"模型 ID 不存在: {model_id}")

    # 3. 校验部署模式
    if model_config.get("deploy_mode") != settings.DEPLOY_MODE:
        raise ValueError(
            f"模型 {model_id} (deploy_mode={model_config.get('deploy_mode')}) "
            f"不属于当前部署模式 {settings.DEPLOY_MODE}"
        )

    if not model_config.get("enabled", True):
        raise ValueError(f"模型 {model_id} 已禁用")

    # 4. 根据 provider_type 创建 Provider
    provider_type = model_config.get("provider_type", "")
    constructor = _PROVIDER_CONSTRUCTORS.get(provider_type)
    if constructor is None:
        raise ValueError(
            f"未知 provider_type: {provider_type}（模型 {model_id}）"
        )

    # 传入 models.json 中的实际 model_id（如 "claude-haiku-4-20250506"）
    actual_model = model_config.get("model_id", "")
    provider = constructor(model=actual_model)

    # 5. 缓存
    _model_provider_cache[model_id] = provider
    log.info(
        "factory.provider_by_model.created",
        model_id=model_id,
        actual_model=actual_model,
        provider_type=provider_type,
    )

    return provider


def clear_model_provider_cache() -> None:
    """清除按 model_id 缓存的 Provider 实例 — 供测试和管理界面使用。"""
    _model_provider_cache.clear()
