"""Provider 故障转移池 — 管理多个同类 Provider 的自动切换。

P2-A Task 3: 当主 Provider 熔断器 OPEN 时，自动切换到链中下一个 Provider。

设计要点：
1. 透明代理 — 实现 Provider 的所有公共方法，调用方无感知
2. 故障转移 — CircuitBreakerOpenError 时自动尝试下一个 Provider
3. 异步生成器特殊处理 — LLM chat() 流式输出中不切换（已 yield 的数据无法撤回）
4. 单例缓存 — 每个 Provider 实例只创建一次，复用 SDK 连接
5. 配置驱动 — 通过 LLM_FAILOVER_CHAIN / EMBEDDER_FAILOVER_CHAIN 等环境变量配置

幂等保障：
- ProviderPool 无状态切换，同一请求内不会重复调用已失败的 Provider
- 故障转移在单次调用内完成，对调用方透明
- 熔断器状态由 circuit_breaker 模块管理，ProviderPool 仅读取

使用示例::

    from app.llm.provider_pool import get_llm_provider_pool

    pool = get_llm_provider_pool()
    async for chunk in pool.chat(messages, stream=True):
        print(chunk)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.llm.registry import get_all_provider_entries
from app.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitState,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()

# Provider 实例缓存 — key: "type:name", value: Provider 实例
_provider_instances: dict[str, Any] = {}


def _get_or_create_provider(name: str, provider_type: str) -> Any:
    """获取或创建 Provider 实例（单例缓存）。

    Args:
        name: Provider 逻辑名称 (e.g., "openai", "vllm")
        provider_type: Provider 类型 ("embedder" | "reranker" | "vectorstore" | "llm")

    Returns:
        Provider 实例

    Raises:
        ValueError: 未找到匹配的 Provider
    """
    cache_key = f"{provider_type}:{name}"
    if cache_key in _provider_instances:
        return _provider_instances[cache_key]

    entries = get_all_provider_entries()
    for entry in entries:
        if entry.name == name and entry.type == provider_type:
            try:
                provider = entry.factory()
                _provider_instances[cache_key] = provider
                log.info(
                    "provider_pool.provider_created",
                    name=name,
                    type=provider_type,
                    breaker=entry.breaker_name,
                )
                return provider
            except Exception as exc:
                log.warning(
                    "provider_pool.create_failed",
                    name=name,
                    type=provider_type,
                    error=str(exc),
                )
                raise

    raise ValueError(
        f"未找到 Provider: name={name}, type={provider_type}。"
        f"请检查 LLM_FAILOVER_CHAIN / EMBEDDER_FAILOVER_CHAIN 等配置。"
    )


def _clear_provider_cache() -> None:
    """清除 Provider 实例缓存 — 供测试使用。"""
    _provider_instances.clear()


class ProviderPool:
    """Provider 故障转移池 — 透明代理，自动切换。

    管理多个同类 Provider，当主 Provider 熔断器 OPEN 时自动切换到下一个。
    对调用方完全透明 — 实现与底层 Provider 相同的方法签名。

    Attributes:
        _providers: Provider 实例列表（按故障转移链顺序）
        _breaker_names: 熔断器名称列表（与 _providers 一一对应）
        _pool_type: 池类型 ("embedder" | "reranker" | "vectorstore" | "llm")
        _current_index: 当前活跃 Provider 索引
    """

    def __init__(
        self,
        providers: list[Any],
        breaker_names: list[str],
        pool_type: str,
    ) -> None:
        assert len(providers) > 0, "ProviderPool 至少需要一个 Provider"
        assert len(providers) == len(breaker_names), "providers 和 breaker_names 长度不一致"
        self._providers = providers
        self._breaker_names = breaker_names
        self._pool_type = pool_type
        self._current_index = 0

    @property
    def current_provider_name(self) -> str:
        """当前活跃 Provider 的熔断器名称。"""
        return self._breaker_names[self._current_index]

    @property
    def provider_count(self) -> int:
        """池中 Provider 数量。"""
        return len(self._providers)

    def _update_current(self, index: int) -> None:
        """更新当前 Provider 索引（发生切换时记录日志）。"""
        if index != self._current_index:
            log.info(
                "provider_pool.failover",
                pool_type=self._pool_type,
                from_provider=self._breaker_names[self._current_index],
                to_provider=self._breaker_names[index],
                from_index=self._current_index,
                to_index=index,
            )
            self._current_index = index

    async def _call_with_failover(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """常规异步方法故障转移 — 遍历 Provider 链，第一个成功的返回结果。

        CircuitBreakerOpenError 触发切换到下一个 Provider；
        其他异常直接抛出（由调用方处理）。

        Args:
            method_name: 要调用的方法名 (e.g., "embed", "rerank", "search")
            *args, **kwargs: 方法参数

        Returns:
            第一个成功 Provider 的返回值

        Raises:
            CircuitBreakerOpenError: 所有 Provider 熔断器均 OPEN
        """
        last_error: CircuitBreakerOpenError | None = None

        for i, provider in enumerate(self._providers):
            name = self._breaker_names[i]
            try:
                method = getattr(provider, method_name)
                result = await method(*args, **kwargs)
                self._update_current(i)
                return result
            except CircuitBreakerOpenError as exc:
                log.warning(
                    "provider_pool.circuit_open",
                    pool_type=self._pool_type,
                    provider=name,
                    method=method_name,
                    error=str(exc),
                )
                last_error = exc
                continue

        # 所有 Provider 熔断 — 抛出最后一个错误
        log.error(
            "provider_pool.all_circuits_open",
            pool_type=self._pool_type,
            method=method_name,
            breakers=self._breaker_names,
        )
        if last_error:
            raise last_error
        raise CircuitBreakerOpenError(
            self._breaker_names[0], CircuitState.OPEN
        )

    async def _astream_with_failover(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """异步生成器故障转移 — 用于 LLM chat() 流式输出。

        特殊处理：如果已经开始 yield 数据，则不再切换 Provider
       （已发送的数据无法撤回，切换会导致内容不连续）。

        Args:
            method_name: 要调用的方法名 (e.g., "chat")
            *args, **kwargs: 方法参数

        Yields:
            第一个成功 Provider 的输出块

        Raises:
            CircuitBreakerOpenError: 所有 Provider 熔断器均 OPEN（且未开始 yield）
        """
        last_error: CircuitBreakerOpenError | None = None

        for i, provider in enumerate(self._providers):
            name = self._breaker_names[i]
            did_yield = False
            try:
                method = getattr(provider, method_name)
                async for chunk in method(*args, **kwargs):
                    did_yield = True
                    yield chunk
                self._update_current(i)
                return  # 成功完成 — 退出
            except CircuitBreakerOpenError as exc:
                if did_yield:
                    # 已开始 yield — 不能切换，抛出错误让调用方处理
                    log.warning(
                        "provider_pool.mid_stream_circuit_open",
                        pool_type=self._pool_type,
                        provider=name,
                        method=method_name,
                        note="已开始输出，无法故障转移",
                    )
                    raise
                log.warning(
                    "provider_pool.circuit_open",
                    pool_type=self._pool_type,
                    provider=name,
                    method=method_name,
                    error=str(exc),
                )
                last_error = exc
                continue

        # 所有 Provider 熔断 — 抛出最后一个错误
        log.error(
            "provider_pool.all_circuits_open",
            pool_type=self._pool_type,
            method=method_name,
            breakers=self._breaker_names,
        )
        if last_error:
            raise last_error
        raise CircuitBreakerOpenError(
            self._breaker_names[0], CircuitState.OPEN
        )

    # ==================================================================
    # Embedder 方法
    # ==================================================================

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """向量嵌入 — 故障转移包装。"""
        return await self._call_with_failover("embed", texts)

    # ==================================================================
    # Reranker 方法
    # ==================================================================

    async def rerank(
        self, query: str, documents: list[Any], top_k: int = 5
    ) -> list[dict]:
        """文档重排 — 故障转移包装。"""
        return await self._call_with_failover("rerank", query, documents, top_k)

    # ==================================================================
    # VectorStore 方法
    # ==================================================================

    async def search(
        self,
        query_vec: list[float],
        kb_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict]:
        """向量检索 — 故障转移包装。"""
        return await self._call_with_failover("search", query_vec, kb_ids, top_k)

    async def upsert(
        self, doc_id: str, chunks: list[Any], **kwargs: Any
    ) -> None:
        """向量写入 — 故障转移包装。"""
        await self._call_with_failover("upsert", doc_id, chunks, **kwargs)

    async def delete(self, doc_id: str) -> None:
        """向量删除 — 故障转移包装。"""
        await self._call_with_failover("delete", doc_id)

    async def health_check(self) -> bool:
        """健康检查 — 故障转移包装。"""
        return await self._call_with_failover("health_check")

    # ==================================================================
    # LLM 方法 — 异步生成器
    # ==================================================================

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        """LLM 对话 — 异步生成器故障转移包装。"""
        async for chunk in self._astream_with_failover(
            "chat", messages, tools, stream, **kwargs
        ):
            yield chunk

    # ==================================================================
    # 透明代理 — 委托其他属性到当前 Provider
    # ==================================================================

    def __getattr__(self, name: str) -> Any:
        """委托非内置属性到当前活跃 Provider。

        用于访问 Provider 的属性（如 default_model、dim 等），
        以及未显式覆盖的方法。
        """
        return getattr(self._providers[self._current_index], name)


# ======================================================================
# 工厂函数 — 从配置创建 ProviderPool
# ======================================================================


def _build_pool(
    pool_type: str,
    failover_chain: str,
    default_factory: Any,
    default_breaker_name: str,
) -> ProviderPool:
    """从故障转移链配置构建 ProviderPool。

    Args:
        pool_type: 池类型 ("embedder" | "reranker" | "vectorstore" | "llm")
        failover_chain: 故障转移链 (e.g., "openai,tei")
        default_factory: 默认 Provider 的工厂函数（链为空时使用）
        default_breaker_name: 默认 Provider 的熔断器名称

    Returns:
        ProviderPool 实例
    """
    if not failover_chain.strip():
        # 无故障转移链 — 包装单个 Provider
        provider = default_factory()
        return ProviderPool(
            providers=[provider],
            breaker_names=[default_breaker_name],
            pool_type=pool_type,
        )

    # 解析故障转移链
    names = [n.strip() for n in failover_chain.split(",") if n.strip()]
    providers: list[Any] = []
    breaker_names: list[str] = []

    entries = get_all_provider_entries()
    entry_map = {(e.type, e.name): e for e in entries}

    for name in names:
        entry = entry_map.get((pool_type, name))
        if entry is None:
            log.warning(
                "provider_pool.skip_unknown_provider",
                pool_type=pool_type,
                name=name,
                chain=failover_chain,
            )
            continue
        try:
            provider = _get_or_create_provider(name, pool_type)
            providers.append(provider)
            breaker_names.append(entry.breaker_name)
        except Exception as exc:
            log.warning(
                "provider_pool.skip_failed_provider",
                pool_type=pool_type,
                name=name,
                error=str(exc),
            )
            continue

    if not providers:
        # 链中所有 Provider 都不可用 — 回退到默认 Provider
        log.warning(
            "provider_pool.fallback_to_default",
            pool_type=pool_type,
            chain=failover_chain,
        )
        provider = default_factory()
        return ProviderPool(
            providers=[provider],
            breaker_names=[default_breaker_name],
            pool_type=pool_type,
        )

    log.info(
        "provider_pool.created",
        pool_type=pool_type,
        providers=breaker_names,
        count=len(providers),
    )

    return ProviderPool(
        providers=providers,
        breaker_names=breaker_names,
        pool_type=pool_type,
    )


@lru_cache
def get_llm_provider_pool() -> ProviderPool:
    """获取 LLM Provider 故障转移池（单例）。

    从 LLM_FAILOVER_CHAIN 配置解析故障转移链，
    链为空时回退到 get_llm_provider() 返回的默认 Provider。

    Returns:
        ProviderPool 实例
    """
    from app.llm.factory import get_llm_provider

    # 确定默认 Provider 的熔断器名称
    mode = settings.DEPLOY_MODE
    default_breaker_map = {
        "saas": "anthropic",
        "saas_dashscope": "dashscope",
        "private_overseas": "vllm",
        "private_domestic": "vllm",
    }
    default_breaker = default_breaker_map.get(mode, "vllm")

    return _build_pool(
        pool_type="llm",
        failover_chain=settings.LLM_FAILOVER_CHAIN,
        default_factory=get_llm_provider,
        default_breaker_name=default_breaker,
    )


@lru_cache
def get_embedder_pool() -> ProviderPool:
    """获取 Embedder 故障转移池（单例）。

    从 EMBEDDER_FAILOVER_CHAIN 配置解析故障转移链，
    链为空时回退到 get_embedder() 返回的默认 Provider。
    """
    from app.llm.embedder import get_embedder

    mode = settings.DEPLOY_MODE
    default_breaker_map = {
        "saas": "embedder_openai",
        "saas_dashscope": "embedder_dashscope",
        "private_overseas": "embedder_tei",
        "private_domestic": "embedder_tei",
    }
    default_breaker = default_breaker_map.get(mode, "embedder_tei")

    return _build_pool(
        pool_type="embedder",
        failover_chain=settings.EMBEDDER_FAILOVER_CHAIN,
        default_factory=get_embedder,
        default_breaker_name=default_breaker,
    )


@lru_cache
def get_reranker_pool() -> ProviderPool:
    """获取 Reranker 故障转移池（单例）。

    从 RERANKER_FAILOVER_CHAIN 配置解析故障转移链，
    链为空时回退到 get_reranker() 返回的默认 Provider。
    """
    from app.rag.reranker import get_reranker

    mode = settings.DEPLOY_MODE
    default_breaker_map = {
        "saas": "reranker_cohere",
        "saas_dashscope": "reranker_cohere",
        "private_overseas": "reranker_tei",
        "private_domestic": "reranker_tei",
    }
    default_breaker = default_breaker_map.get(mode, "reranker_tei")

    return _build_pool(
        pool_type="reranker",
        failover_chain=settings.RERANKER_FAILOVER_CHAIN,
        default_factory=get_reranker,
        default_breaker_name=default_breaker,
    )


@lru_cache
def get_vector_store_pool() -> ProviderPool:
    """获取 VectorStore 故障转移池（单例）。

    从 VECTOR_STORE_FAILOVER_CHAIN 配置解析故障转移链，
    链为空时回退到 get_vector_store() 返回的默认 Provider。
    """
    from app.rag.vector_store import get_vector_store

    default_breaker = (
        "vectorstore_milvus" if settings.VECTOR_STORE == "milvus" else "vectorstore_opensearch"
    )

    return _build_pool(
        pool_type="vectorstore",
        failover_chain=settings.VECTOR_STORE_FAILOVER_CHAIN,
        default_factory=get_vector_store,
        default_breaker_name=default_breaker,
    )


def clear_pool_cache() -> None:
    """清除 ProviderPool 单例缓存 — 供测试使用。"""
    get_llm_provider_pool.cache_clear()
    get_embedder_pool.cache_clear()
    get_reranker_pool.cache_clear()
    get_vector_store_pool.cache_clear()
    _clear_provider_cache()
