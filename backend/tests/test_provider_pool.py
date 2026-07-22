"""P2-A Task 3: ProviderPool 故障转移池测试。

测试覆盖：
    1. ProviderPool 基本属性
    2. _call_with_failover — 常规异步方法故障转移
    3. _astream_with_failover — 异步生成器故障转移
    4. embed/rerank/search/chat 方法委托
    5. 全部熔断场景
    6. 透明代理 __getattr__
    7. 工厂函数 — get_llm_provider_pool / get_embedder_pool 等
    8. _build_pool — 从配置链构建
    9. 单例缓存
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.provider_pool import (
    ProviderPool,
    _build_pool,
    _get_or_create_provider,
    clear_pool_cache,
    get_embedder_pool,
    get_llm_provider_pool,
    get_reranker_pool,
    get_vector_store_pool,
)
from app.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitState,
    _breakers,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


# ======================================================================
# 测试辅助
# ======================================================================


def _make_mock_provider(name: str, embed_result=None):
    """创建 Mock Provider。"""
    provider = MagicMock()
    provider._name = name
    provider.embed = AsyncMock(
        return_value=embed_result or [[0.1, 0.2, 0.3]]
    )
    provider.rerank = AsyncMock(return_value=[{"index": 0, "score": 0.9}])
    provider.search = AsyncMock(return_value=[{"id": "1", "score": 0.8}])
    provider.health_check = AsyncMock(return_value=True)
    provider.upsert = AsyncMock(return_value=None)
    provider.delete = AsyncMock(return_value=None)
    return provider


def _make_mock_llm_provider(name: str):
    """创建 Mock LLM Provider（async generator chat）。"""
    provider = MagicMock()
    provider._name = name
    provider.default_model = f"model-{name}"

    async def _chat(messages, tools=None, stream=False, **kwargs):
        yield f"chunk-from-{name}-1"
        yield f"chunk-from-{name}-2"

    provider.chat = _chat
    return provider


# ======================================================================
# ProviderPool 基本属性测试
# ======================================================================


class TestProviderPoolBasic:
    """ProviderPool 基本属性和构造测试。"""

    def test_create_pool_with_single_provider(self):
        """单 Provider 池正常创建。"""
        provider = _make_mock_provider("test")
        pool = ProviderPool([provider], ["test_breaker"], "embedder")
        assert pool.provider_count == 1
        assert pool.current_provider_name == "test_breaker"

    def test_create_pool_with_multiple_providers(self):
        """多 Provider 池正常创建。"""
        p1 = _make_mock_provider("p1")
        p2 = _make_mock_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")
        assert pool.provider_count == 2
        assert pool.current_provider_name == "b1"

    def test_empty_pool_raises_assertion(self):
        """空池抛出断言错误。"""
        with pytest.raises(AssertionError):
            ProviderPool([], [], "embedder")

    def test_mismatched_lengths_raises_assertion(self):
        """providers 和 breaker_names 长度不一致抛出断言错误。"""
        p1 = _make_mock_provider("p1")
        with pytest.raises(AssertionError):
            ProviderPool([p1], ["b1", "b2"], "embedder")


# ======================================================================
# _call_with_failover 测试
# ======================================================================


class TestCallWithFailover:
    """常规异步方法故障转移测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_primary_succeeds_no_failover(self):
        """主 Provider 成功时不切换。"""
        p1 = _make_mock_provider("p1")
        p2 = _make_mock_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        result = await pool._call_with_failover("embed", ["test"])
        assert result == [[0.1, 0.2, 0.3]]
        p1.embed.assert_called_once()
        p2.embed.assert_not_called()
        assert pool.current_provider_name == "b1"

    async def test_failover_on_circuit_open(self):
        """主 Provider 熔断时切换到备用。"""
        p1 = _make_mock_provider("p1")
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_mock_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        result = await pool._call_with_failover("embed", ["test"])
        assert result == [[0.1, 0.2, 0.3]]
        p1.embed.assert_called_once()
        p2.embed.assert_called_once()
        assert pool.current_provider_name == "b2"

    async def test_all_circuits_open_raises(self):
        """所有 Provider 熔断时抛出异常。"""
        p1 = _make_mock_provider("p1")
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_mock_provider("p2")
        p2.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b2", CircuitState.OPEN)
        )
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        with pytest.raises(CircuitBreakerOpenError):
            await pool._call_with_failover("embed", ["test"])

    async def test_non_circuit_error_propagates(self):
        """非熔断异常直接传播（不切换）。"""
        p1 = _make_mock_provider("p1")
        p1.embed = AsyncMock(side_effect=ValueError("api error"))
        p2 = _make_mock_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        with pytest.raises(ValueError, match="api error"):
            await pool._call_with_failover("embed", ["test"])
        p2.embed.assert_not_called()

    async def test_failover_updates_current_index(self):
        """故障转移后 current_index 更新。"""
        p1 = _make_mock_provider("p1")
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_mock_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        assert pool.current_provider_name == "b1"
        await pool._call_with_failover("embed", ["test"])
        assert pool.current_provider_name == "b2"

    async def test_three_provider_chain_failover(self):
        """三 Provider 链式故障转移。"""
        p1 = _make_mock_provider("p1")
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_mock_provider("p2")
        p2.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b2", CircuitState.OPEN)
        )
        p3 = _make_mock_provider("p3")
        pool = ProviderPool([p1, p2, p3], ["b1", "b2", "b3"], "embedder")

        result = await pool._call_with_failover("embed", ["test"])
        assert result == [[0.1, 0.2, 0.3]]
        assert pool.current_provider_name == "b3"


# ======================================================================
# _astream_with_failover 测试
# ======================================================================


class TestAstreamWithFailover:
    """异步生成器故障转移测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_primary_stream_succeeds(self):
        """主 Provider 流式成功。"""
        p1 = _make_mock_llm_provider("p1")
        p2 = _make_mock_llm_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "llm")

        chunks = []
        async for chunk in pool._astream_with_failover("chat", [], stream=True):
            chunks.append(chunk)

        assert chunks == ["chunk-from-p1-1", "chunk-from-p1-2"]
        assert pool.current_provider_name == "b1"

    async def test_stream_failover_before_first_yield(self):
        """流开始前熔断 → 切换到备用 Provider。"""
        p1 = MagicMock()

        async def _failing_chat(messages, tools=None, stream=False, **kwargs):
            raise CircuitBreakerOpenError("b1", CircuitState.OPEN)
            yield  # noqa: unreachable — 使其成为 async generator

        p1.chat = _failing_chat
        p2 = _make_mock_llm_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "llm")

        chunks = []
        async for chunk in pool._astream_with_failover("chat", [], stream=True):
            chunks.append(chunk)

        assert chunks == ["chunk-from-p2-1", "chunk-from-p2-2"]
        assert pool.current_provider_name == "b2"

    async def test_stream_no_failover_after_first_yield(self):
        """已开始 yield 后熔断 → 不切换，抛出异常。"""
        p1 = MagicMock()

        async def _mid_stream_fail(messages, tools=None, stream=False, **kwargs):
            yield "chunk-1"
            raise CircuitBreakerOpenError("b1", CircuitState.OPEN)

        p1.chat = _mid_stream_fail
        p2 = _make_mock_llm_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "llm")

        chunks = []
        with pytest.raises(CircuitBreakerOpenError):
            async for chunk in pool._astream_with_failover("chat", [], stream=True):
                chunks.append(chunk)

        assert chunks == ["chunk-1"]  # 只收到第一个 chunk

    async def test_all_streams_circuit_open(self):
        """所有 Provider 熔断 → 抛出异常。"""
        p1 = MagicMock()

        async def _fail_chat_1(messages, tools=None, stream=False, **kwargs):
            raise CircuitBreakerOpenError("b1", CircuitState.OPEN)
            yield  # noqa

        p1.chat = _fail_chat_1

        p2 = MagicMock()

        async def _fail_chat_2(messages, tools=None, stream=False, **kwargs):
            raise CircuitBreakerOpenError("b2", CircuitState.OPEN)
            yield  # noqa

        p2.chat = _fail_chat_2

        pool = ProviderPool([p1, p2], ["b1", "b2"], "llm")

        with pytest.raises(CircuitBreakerOpenError):
            async for _ in pool._astream_with_failover("chat", [], stream=True):
                pass


# ======================================================================
# 公共方法委托测试
# ======================================================================


class TestMethodDelegation:
    """embed/rerank/search/chat 方法委托测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_embed_delegates_to_call_with_failover(self):
        """embed 方法委托到 _call_with_failover。"""
        p1 = _make_mock_provider("p1", embed_result=[[0.5, 0.6]])
        pool = ProviderPool([p1], ["b1"], "embedder")

        result = await pool.embed(["test text"])
        assert result == [[0.5, 0.6]]
        p1.embed.assert_called_once_with(["test text"])

    async def test_rerank_delegates(self):
        """rerank 方法委托到 _call_with_failover。"""
        p1 = _make_mock_provider("p1")
        pool = ProviderPool([p1], ["b1"], "reranker")

        result = await pool.rerank("query", ["doc1", "doc2"], top_k=3)
        assert result == [{"index": 0, "score": 0.9}]
        p1.rerank.assert_called_once_with("query", ["doc1", "doc2"], 3)

    async def test_search_delegates(self):
        """search 方法委托到 _call_with_failover。"""
        p1 = _make_mock_provider("p1")
        pool = ProviderPool([p1], ["b1"], "vectorstore")

        result = await pool.search([0.1, 0.2], kb_ids=["kb1"], top_k=10)
        assert result == [{"id": "1", "score": 0.8}]
        p1.search.assert_called_once_with([0.1, 0.2], ["kb1"], 10)

    async def test_health_check_delegates(self):
        """health_check 方法委托到 _call_with_failover。"""
        p1 = _make_mock_provider("p1")
        pool = ProviderPool([p1], ["b1"], "vectorstore")

        result = await pool.health_check()
        assert result is True
        p1.health_check.assert_called_once()

    async def test_upsert_delegates(self):
        """upsert 方法委托到 _call_with_failover。"""
        p1 = _make_mock_provider("p1")
        pool = ProviderPool([p1], ["b1"], "vectorstore")

        await pool.upsert("doc-1", [])
        p1.upsert.assert_called_once()

    async def test_delete_delegates(self):
        """delete 方法委托到 _call_with_failover。"""
        p1 = _make_mock_provider("p1")
        pool = ProviderPool([p1], ["b1"], "vectorstore")

        await pool.delete("doc-1")
        p1.delete.assert_called_once_with("doc-1")

    async def test_chat_delegates_to_astream(self):
        """chat 方法委托到 _astream_with_failover。"""
        p1 = _make_mock_llm_provider("p1")
        pool = ProviderPool([p1], ["b1"], "llm")

        chunks = []
        async for chunk in pool.chat([], stream=True):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert "p1" in chunks[0]


# ======================================================================
# 透明代理 __getattr__ 测试
# ======================================================================


class TestTransparentProxy:
    """__getattr__ 透明代理测试。"""

    def test_attribute_access_delegates_to_current(self):
        """属性访问委托到当前 Provider。"""
        p1 = _make_mock_llm_provider("p1")
        p1.custom_attr = "value"
        pool = ProviderPool([p1], ["b1"], "llm")

        assert pool.custom_attr == "value"

    def test_attribute_access_after_failover(self):
        """故障转移后属性访问指向新 Provider。"""
        p1 = _make_mock_llm_provider("p1")
        p1.default_model = "model-p1"
        p2 = _make_mock_llm_provider("p2")
        p2.default_model = "model-p2"
        pool = ProviderPool([p1, p2], ["b1", "b2"], "llm")

        assert pool.default_model == "model-p1"

        # 手动切换
        pool._current_index = 1
        assert pool.default_model == "model-p2"

    def test_missing_attribute_raises_attribute_error(self):
        """不存在的属性抛出 AttributeError。"""
        p1 = MagicMock(spec=["embed", "rerank", "search", "health_check"])
        pool = ProviderPool([p1], ["b1"], "embedder")

        with pytest.raises(AttributeError):
            _ = pool.nonexistent_attribute


# ======================================================================
# _build_pool 工厂构建测试
# ======================================================================


class TestBuildPool:
    """_build_pool 从配置链构建测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()
        clear_pool_cache()

    def test_empty_chain_uses_default_provider(self):
        """空故障转移链 → 使用默认 Provider。"""
        default_factory = MagicMock(return_value=_make_mock_provider("default"))

        pool = _build_pool(
            pool_type="embedder",
            failover_chain="",
            default_factory=default_factory,
            default_breaker_name="default_breaker",
        )

        assert pool.provider_count == 1
        assert pool.current_provider_name == "default_breaker"
        default_factory.assert_called_once()

    def test_whitespace_chain_uses_default(self):
        """纯空白链 → 使用默认 Provider。"""
        default_factory = MagicMock(return_value=_make_mock_provider("default"))

        pool = _build_pool(
            pool_type="embedder",
            failover_chain="   ",
            default_factory=default_factory,
            default_breaker_name="default_breaker",
        )

        assert pool.provider_count == 1

    def test_chain_with_unknown_providers_falls_back_to_default(self):
        """链中所有 Provider 未知 → 回退到默认。"""
        default_factory = MagicMock(return_value=_make_mock_provider("default"))

        with patch(
            "app.llm.provider_pool.get_all_provider_entries",
            return_value=[],
        ):
            pool = _build_pool(
                pool_type="embedder",
                failover_chain="unknown1,unknown2",
                default_factory=default_factory,
                default_breaker_name="default_breaker",
            )

        assert pool.provider_count == 1
        assert pool.current_provider_name == "default_breaker"


# ======================================================================
# 工厂函数测试
# ======================================================================


class TestFactoryFunctions:
    """get_*_pool 工厂函数测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()
        clear_pool_cache()

    def test_get_llm_provider_pool_returns_pool(self):
        """get_llm_provider_pool 返回 ProviderPool。"""
        pool = get_llm_provider_pool()
        assert isinstance(pool, ProviderPool)
        assert pool.provider_count >= 1

    def test_get_embedder_pool_returns_pool(self):
        """get_embedder_pool 返回 ProviderPool。"""
        pool = get_embedder_pool()
        assert isinstance(pool, ProviderPool)
        assert pool.provider_count >= 1

    def test_get_reranker_pool_returns_pool(self):
        """get_reranker_pool 返回 ProviderPool。"""
        # Cohere 未安装时使用 mock 避免导入错误
        try:
            import cohere  # noqa: F401
        except ImportError:
            # cohere 未安装 — mock get_reranker 返回一个 mock provider
            mock_reranker = _make_mock_provider("mock_reranker")
            with patch("app.rag.reranker.get_reranker", return_value=mock_reranker):
                pool = get_reranker_pool()
            assert isinstance(pool, ProviderPool)
            assert pool.provider_count >= 1
            return

        pool = get_reranker_pool()
        assert isinstance(pool, ProviderPool)
        assert pool.provider_count >= 1

    def test_get_vector_store_pool_returns_pool(self):
        """get_vector_store_pool 返回 ProviderPool。"""
        pool = get_vector_store_pool()
        assert isinstance(pool, ProviderPool)
        assert pool.provider_count >= 1

    def test_pool_is_singleton(self):
        """工厂函数返回单例（lru_cache）。"""
        pool1 = get_llm_provider_pool()
        pool2 = get_llm_provider_pool()
        assert pool1 is pool2

    def test_clear_pool_cache_creates_new_instance(self):
        """clear_pool_cache 后创建新实例。"""
        pool1 = get_llm_provider_pool()
        clear_pool_cache()
        pool2 = get_llm_provider_pool()
        assert pool1 is not pool2


# ======================================================================
# _get_or_create_provider 测试
# ======================================================================


class TestGetOrCreateProvider:
    """_get_or_create_provider 单例缓存测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()
        clear_pool_cache()

    def test_returns_provider_instance(self):
        """返回 Provider 实例。"""
        # OpenAI Embedder 应该在测试环境中可用（dummy key）
        provider = _get_or_create_provider("openai", "embedder")
        assert provider is not None

    def test_cached_provider_is_same_instance(self):
        """缓存返回同一实例。"""
        p1 = _get_or_create_provider("openai", "embedder")
        p2 = _get_or_create_provider("openai", "embedder")
        assert p1 is p2

    def test_unknown_provider_raises(self):
        """未知 Provider 抛出 ValueError。"""
        with pytest.raises(ValueError, match="未找到 Provider"):
            _get_or_create_provider("nonexistent", "embedder")


# ======================================================================
# 端到端故障转移场景测试
# ======================================================================


class TestEndToEndFailover:
    """端到端故障转移场景测试。"""

    def setup_method(self):
        reset_all_circuit_breakers()

    async def test_embed_failover_e2e(self):
        """Embedder 故障转移端到端测试。"""
        p1 = _make_mock_provider("p1")
        p1.embed = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_mock_provider("p2", embed_result=[[0.9, 0.8]])
        pool = ProviderPool([p1, p2], ["b1", "b2"], "embedder")

        result = await pool.embed(["test"])
        assert result == [[0.9, 0.8]]
        assert pool.current_provider_name == "b2"

    async def test_chat_failover_e2e(self):
        """LLM chat 故障转移端到端测试。"""
        p1 = MagicMock()

        async def _fail(messages, tools=None, stream=False, **kwargs):
            raise CircuitBreakerOpenError("b1", CircuitState.OPEN)
            yield  # noqa

        p1.chat = _fail
        p2 = _make_mock_llm_provider("p2")
        pool = ProviderPool([p1, p2], ["b1", "b2"], "llm")

        chunks = []
        async for chunk in pool.chat([], stream=True):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert "p2" in chunks[0]
        assert pool.current_provider_name == "b2"

    async def test_rerank_failover_e2e(self):
        """Reranker 故障转移端到端测试。"""
        p1 = _make_mock_provider("p1")
        p1.rerank = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_mock_provider("p2")
        p2.rerank = AsyncMock(return_value=[{"index": 1, "score": 0.95}])
        pool = ProviderPool([p1, p2], ["b1", "b2"], "reranker")

        result = await pool.rerank("query", ["doc1"], top_k=1)
        assert result == [{"index": 1, "score": 0.95}]
        assert pool.current_provider_name == "b2"

    async def test_search_failover_e2e(self):
        """VectorStore search 故障转移端到端测试。"""
        p1 = _make_mock_provider("p1")
        p1.search = AsyncMock(
            side_effect=CircuitBreakerOpenError("b1", CircuitState.OPEN)
        )
        p2 = _make_mock_provider("p2")
        p2.search = AsyncMock(return_value=[{"id": "2", "score": 0.99}])
        pool = ProviderPool([p1, p2], ["b1", "b2"], "vectorstore")

        result = await pool.search([0.1, 0.2], top_k=5)
        assert result == [{"id": "2", "score": 0.99}]
        assert pool.current_provider_name == "b2"
