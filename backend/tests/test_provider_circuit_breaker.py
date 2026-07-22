"""P2-A Task 1: Provider 熔断器覆盖测试

验证所有 AI 服务 Provider 的熔断器集成：
- Embedder (OpenAI/TEI/DashScope)
- Reranker (Cohere/TEI)
- VectorStore (OpenSearch/Milvus)
- LLM Provider (VLLM/Anthropic) — 补充 provider 级别测试

测试场景：
1. 正常调用 — 熔断器保持 CLOSED
2. 连续失败达阈值 — 熔断器 OPEN
3. 熔断器 OPEN — 快速拒绝（CircuitBreakerOpenError）
4. 恢复超时后 — HALF_OPEN → 探测成功 → CLOSED
5. 恢复超时后 — HALF_OPEN → 探测失败 → 重新 OPEN
6. 间歇性失败 — 不触发熔断（failure_count 重置）
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# 设置测试用 dummy API key — 避免构造 Provider 时报错
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")

from app.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitState,
    _breakers,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_breakers():
    """每个测试前重置所有熔断器。"""
    reset_all_circuit_breakers()
    yield
    reset_all_circuit_breakers()


def _configure_breaker(name: str, threshold: int = 3, recovery: float = 0.1):
    """配置测试用熔断器参数 — 降低阈值和恢复时间方便测试。"""
    cb = get_circuit_breaker(name)
    cb.failure_threshold = threshold
    cb.recovery_timeout = recovery
    return cb


class _MockAsyncStream:
    """模拟 OpenAI streaming response 的 async iterator。"""

    def __init__(self, chunks: list):
        self._chunks = chunks
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


# ---------------------------------------------------------------------------
# Embedder 熔断器测试
# ---------------------------------------------------------------------------

class TestOpenAIEmbedderCircuitBreaker:
    """OpenAIEmbedder 熔断器集成测试。"""

    def test_success_keeps_closed(self):
        """正常调用 — 熔断器保持 CLOSED。"""
        from app.llm.embedder import OpenAIEmbedder

        emb = OpenAIEmbedder()
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.1] * 3072)]
        emb.client.embeddings.create = AsyncMock(return_value=mock_resp)

        result = asyncio.run(emb.embed(["test"]))
        assert len(result) == 1
        assert len(result[0]) == 3072

        cb = get_circuit_breaker("embedder_openai")
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_failures_open_circuit(self):
        """连续失败达阈值 — 熔断器 OPEN。"""
        from app.llm.embedder import OpenAIEmbedder

        emb = OpenAIEmbedder()
        emb.client.embeddings.create = AsyncMock(side_effect=RuntimeError("API timeout"))
        _configure_breaker("embedder_openai", threshold=3)

        for _ in range(3):
            with pytest.raises(RuntimeError):
                asyncio.run(emb.embed(["test"]))

        cb = get_circuit_breaker("embedder_openai")
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    def test_open_circuit_fast_reject(self):
        """熔断器 OPEN — 快速拒绝，不调用底层 API。"""
        from app.llm.embedder import OpenAIEmbedder

        emb = OpenAIEmbedder()
        call_count = 0

        async def failing_create(**kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("API timeout")

        emb.client.embeddings.create = failing_create
        _configure_breaker("embedder_openai", threshold=2)

        # 触发熔断
        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(emb.embed(["test"]))

        assert call_count == 2
        cb = get_circuit_breaker("embedder_openai")
        assert cb.state == CircuitState.OPEN

        # 熔断后调用 — 应快速拒绝，不增加 call_count
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(emb.embed(["test"]))
        assert call_count == 2  # 底层 API 未被调用

    def test_recovery_success_closes_circuit(self):
        """恢复超时后 HALF_OPEN → 探测成功 → CLOSED。"""
        from app.llm.embedder import OpenAIEmbedder

        emb = OpenAIEmbedder()
        emb.client.embeddings.create = AsyncMock(side_effect=RuntimeError("API timeout"))
        _configure_breaker("embedder_openai", threshold=2, recovery=0.05)

        # 触发熔断
        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(emb.embed(["test"]))

        cb = get_circuit_breaker("embedder_openai")
        assert cb.state == CircuitState.OPEN

        # 等待恢复超时
        time.sleep(0.06)

        # 恢复正常
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.1] * 3072)]
        emb.client.embeddings.create = AsyncMock(return_value=mock_resp)

        result = asyncio.run(emb.embed(["test"]))
        assert len(result) == 1
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_recovery_failure_reopens_circuit(self):
        """恢复超时后 HALF_OPEN → 探测失败 → 重新 OPEN。"""
        from app.llm.embedder import OpenAIEmbedder

        emb = OpenAIEmbedder()
        emb.client.embeddings.create = AsyncMock(side_effect=RuntimeError("Still down"))
        _configure_breaker("embedder_openai", threshold=2, recovery=0.05)

        # 触发熔断
        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(emb.embed(["test"]))

        cb = get_circuit_breaker("embedder_openai")
        assert cb.state == CircuitState.OPEN

        # 等待恢复超时
        time.sleep(0.06)

        # 探测仍然失败
        with pytest.raises(RuntimeError):
            asyncio.run(emb.embed(["test"]))
        assert cb.state == CircuitState.OPEN

    def test_intermittent_failure_no_trip(self):
        """间歇性失败 — 成功重置 failure_count，不触发熔断。"""
        from app.llm.embedder import OpenAIEmbedder

        emb = OpenAIEmbedder()
        _configure_breaker("embedder_openai", threshold=3)

        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.1] * 3072)]

        call_count = 0

        async def intermittent(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise RuntimeError("Transient error")
            return mock_resp

        emb.client.embeddings.create = intermittent

        # fail, success, fail, success, fail, success — 间歇性，不连续达阈值
        for i in range(6):
            try:
                asyncio.run(emb.embed(["test"]))
            except RuntimeError:
                pass

        cb = get_circuit_breaker("embedder_openai")
        assert cb.state == CircuitState.CLOSED


class TestTEIEmbedderCircuitBreaker:
    """TEIEmbedder 熔断器集成测试。"""

    def test_failures_open_circuit(self):
        from app.llm.embedder import TEIEmbedder

        emb = TEIEmbedder()
        emb.client.post = AsyncMock(side_effect=RuntimeError("Connection refused"))
        _configure_breaker("embedder_tei", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(emb.embed(["test"]))

        cb = get_circuit_breaker("embedder_tei")
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_fast_reject(self):
        from app.llm.embedder import TEIEmbedder

        emb = TEIEmbedder()
        emb.client.post = AsyncMock(side_effect=RuntimeError("Connection refused"))
        _configure_breaker("embedder_tei", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(emb.embed(["test"]))

        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(emb.embed(["test"]))


class TestDashScopeEmbedderCircuitBreaker:
    """DashScopeEmbedder 熔断器集成测试。"""

    def test_failures_open_circuit(self):
        from app.llm.embedder import DashScopeEmbedder

        emb = DashScopeEmbedder()
        emb.client.embeddings.create = AsyncMock(side_effect=RuntimeError("API timeout"))
        _configure_breaker("embedder_dashscope", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(emb.embed(["test"]))

        cb = get_circuit_breaker("embedder_dashscope")
        assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# Reranker 熔断器测试
# ---------------------------------------------------------------------------

class TestCohereRerankerCircuitBreaker:
    """CohereReranker 熔断器集成测试。"""

    @pytest.fixture(autouse=True)
    def _check_cohere(self):
        """cohere 未安装时跳过。"""
        pytest.importorskip("cohere")

    def test_success_keeps_closed(self):
        from app.rag.reranker import CohereReranker

        reranker = CohereReranker()
        mock_resp = MagicMock()
        mock_resp.results = [MagicMock(index=0, relevance_score=0.95)]
        reranker.client.rerank = AsyncMock(return_value=mock_resp)

        result = asyncio.run(reranker.rerank("query", ["doc1"], top_k=1))
        assert len(result) == 1
        assert result[0]["score"] == 0.95

        cb = get_circuit_breaker("reranker_cohere")
        assert cb.state == CircuitState.CLOSED

    def test_failures_open_circuit(self):
        from app.rag.reranker import CohereReranker

        reranker = CohereReranker()
        reranker.client.rerank = AsyncMock(side_effect=RuntimeError("Cohere API error"))
        _configure_breaker("reranker_cohere", threshold=3)

        for _ in range(3):
            with pytest.raises(RuntimeError):
                asyncio.run(reranker.rerank("query", ["doc1"], top_k=1))

        cb = get_circuit_breaker("reranker_cohere")
        assert cb.state == CircuitState.OPEN

    def test_recovery_success_closes(self):
        from app.rag.reranker import CohereReranker

        reranker = CohereReranker()
        reranker.client.rerank = AsyncMock(side_effect=RuntimeError("API error"))
        _configure_breaker("reranker_cohere", threshold=2, recovery=0.05)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(reranker.rerank("query", ["doc1"], top_k=1))

        time.sleep(0.06)

        mock_resp = MagicMock()
        mock_resp.results = [MagicMock(index=0, relevance_score=0.9)]
        reranker.client.rerank = AsyncMock(return_value=mock_resp)

        result = asyncio.run(reranker.rerank("query", ["doc1"], top_k=1))
        assert len(result) == 1

        cb = get_circuit_breaker("reranker_cohere")
        assert cb.state == CircuitState.CLOSED


class TestTEIRerankerCircuitBreaker:
    """TEIReranker 熔断器集成测试。"""

    def test_failures_open_circuit(self):
        from app.rag.reranker import TEIReranker

        reranker = TEIReranker()
        reranker.client.post = AsyncMock(side_effect=RuntimeError("Connection refused"))
        _configure_breaker("reranker_tei", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(reranker.rerank("query", ["doc1"], top_k=1))

        cb = get_circuit_breaker("reranker_tei")
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_fast_reject(self):
        from app.rag.reranker import TEIReranker

        reranker = TEIReranker()
        reranker.client.post = AsyncMock(side_effect=RuntimeError("Connection refused"))
        _configure_breaker("reranker_tei", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(reranker.rerank("query", ["doc1"], top_k=1))

        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(reranker.rerank("query", ["doc1"], top_k=1))


# ---------------------------------------------------------------------------
# VectorStore 熔断器测试
# ---------------------------------------------------------------------------

class TestOpenSearchStoreCircuitBreaker:
    """OpenSearchVectorStore 熔断器集成测试。"""

    def test_success_keeps_closed(self):
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        store = OpenSearchVectorStore()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"hits": {"hits": []}}
        mock_resp.raise_for_status = MagicMock()
        store._http.post = AsyncMock(return_value=mock_resp)

        result = asyncio.run(store.search([0.1] * 1024, top_k=5))
        assert result == []

        cb = get_circuit_breaker("vectorstore_opensearch")
        assert cb.state == CircuitState.CLOSED

    def test_failures_open_circuit(self):
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        store = OpenSearchVectorStore()
        store._http.post = AsyncMock(side_effect=RuntimeError("OpenSearch unreachable"))
        _configure_breaker("vectorstore_opensearch", threshold=3)

        for _ in range(3):
            with pytest.raises(RuntimeError):
                asyncio.run(store.search([0.1] * 1024, top_k=5))

        cb = get_circuit_breaker("vectorstore_opensearch")
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_fast_reject(self):
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        store = OpenSearchVectorStore()
        call_count = 0

        async def failing_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("OpenSearch unreachable")

        store._http.post = failing_post
        _configure_breaker("vectorstore_opensearch", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(store.search([0.1] * 1024, top_k=5))

        assert call_count == 2

        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(store.search([0.1] * 1024, top_k=5))
        assert call_count == 2  # 底层 API 未被调用

    def test_recovery_success_closes(self):
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

        store = OpenSearchVectorStore()
        store._http.post = AsyncMock(side_effect=RuntimeError("OpenSearch unreachable"))
        _configure_breaker("vectorstore_opensearch", threshold=2, recovery=0.05)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(store.search([0.1] * 1024, top_k=5))

        time.sleep(0.06)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"hits": {"hits": []}}
        mock_resp.raise_for_status = MagicMock()
        store._http.post = AsyncMock(return_value=mock_resp)

        result = asyncio.run(store.search([0.1] * 1024, top_k=5))
        assert result == []

        cb = get_circuit_breaker("vectorstore_opensearch")
        assert cb.state == CircuitState.CLOSED


class TestMilvusStoreCircuitBreaker:
    """MilvusVectorStore 熔断器集成测试。"""

    def test_failures_open_circuit(self):
        from app.rag.vector_store.milvus_store import MilvusVectorStore

        store = MilvusVectorStore()
        store._http.post = AsyncMock(side_effect=RuntimeError("Milvus unreachable"))
        _configure_breaker("vectorstore_milvus", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(store.search([0.1] * 1024, top_k=5))

        cb = get_circuit_breaker("vectorstore_milvus")
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_fast_reject(self):
        from app.rag.vector_store.milvus_store import MilvusVectorStore

        store = MilvusVectorStore()
        store._http.post = AsyncMock(side_effect=RuntimeError("Milvus unreachable"))
        _configure_breaker("vectorstore_milvus", threshold=2)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(store.search([0.1] * 1024, top_k=5))

        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(store.search([0.1] * 1024, top_k=5))


# ---------------------------------------------------------------------------
# LLM Provider 熔断器测试（补充 provider 级别）
# ---------------------------------------------------------------------------

class TestVLLMProviderCircuitBreaker:
    """VLLMProvider 熔断器集成测试 — chat() 是 async generator。"""

    def test_success_keeps_closed(self):
        from app.llm.vllm_provider import VLLMProvider

        provider = VLLMProvider()
        mock_chunk = MagicMock()
        mock_chunk.choices = [MagicMock(delta=MagicMock(content="hello", tool_calls=None))]
        mock_resp = _MockAsyncStream([mock_chunk])
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        async def consume():
            async for token in provider.chat([{"role": "user", "content": "hi"}], stream=True):
                pass

        asyncio.run(consume())

        cb = get_circuit_breaker("vllm")
        assert cb.state == CircuitState.CLOSED

    def test_failures_open_circuit(self):
        from app.llm.vllm_provider import VLLMProvider

        provider = VLLMProvider()
        provider.client.chat.completions.create = AsyncMock(side_effect=RuntimeError("vLLM down"))
        _configure_breaker("vllm", threshold=2)

        async def consume():
            async for _ in provider.chat([{"role": "user", "content": "hi"}], stream=True):
                pass

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(consume())

        cb = get_circuit_breaker("vllm")
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_fast_reject(self):
        from app.llm.vllm_provider import VLLMProvider

        provider = VLLMProvider()
        provider.client.chat.completions.create = AsyncMock(side_effect=RuntimeError("vLLM down"))
        _configure_breaker("vllm", threshold=2)

        async def consume():
            async for _ in provider.chat([{"role": "user", "content": "hi"}], stream=True):
                pass

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(consume())

        # 熔断后快速拒绝
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(consume())


class TestAnthropicProviderCircuitBreaker:
    """AnthropicProvider 熔断器集成测试。"""

    def test_failures_open_circuit(self):
        from app.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider()
        # mock messages.stream to raise
        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("Anthropic API error"))
        provider.client.messages.stream = MagicMock(return_value=mock_stream_ctx)
        _configure_breaker("anthropic", threshold=2)

        async def consume():
            async for _ in provider.chat([{"role": "user", "content": "hi"}], stream=True):
                pass

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(consume())

        cb = get_circuit_breaker("anthropic")
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_fast_reject(self):
        from app.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider()
        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("Anthropic API error"))
        provider.client.messages.stream = MagicMock(return_value=mock_stream_ctx)
        _configure_breaker("anthropic", threshold=2)

        async def consume():
            async for _ in provider.chat([{"role": "user", "content": "hi"}], stream=True):
                pass

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(consume())

        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(consume())


# ---------------------------------------------------------------------------
# 熔断器注册验证
# ---------------------------------------------------------------------------

class TestCircuitBreakerRegistration:
    """验证所有 Provider 的熔断器在模块加载时正确注册。"""

    def test_all_breakers_registered(self):
        """所有 Provider 熔断器名称在注册表中存在。"""
        # 触发模块加载 — 包括 DashScope
        from app.llm.embedder import OpenAIEmbedder, TEIEmbedder, DashScopeEmbedder
        from app.rag.reranker import CohereReranker, TEIReranker
        from app.rag.vector_store.opensearch_store import OpenSearchVectorStore
        from app.rag.vector_store.milvus_store import MilvusVectorStore
        from app.llm.vllm_provider import VLLMProvider
        from app.llm.anthropic_provider import AnthropicProvider
        try:
            from app.llm.dashscope_provider import DashScopeProvider
        except ImportError:
            pass  # DashScopeProvider 可能未安装

        expected_names = [
            "embedder_openai",
            "embedder_tei",
            "embedder_dashscope",
            "reranker_cohere",
            "reranker_tei",
            "vectorstore_opensearch",
            "vectorstore_milvus",
            "vllm",
            "anthropic",
        ]
        # dashscope LLM breaker 仅在 DashScopeProvider 实例化时注册（inline 模式）
        try:
            from app.llm.dashscope_provider import DashScopeProvider
            DashScopeProvider()  # 触发 __init__ → get_circuit_breaker("dashscope")
            expected_names.append("dashscope")
        except (ImportError, Exception):
            pass

        for name in expected_names:
            assert name in _breakers, f"熔断器 '{name}' 未在注册表中找到"
            cb = _breakers[name]
            assert cb.state == CircuitState.CLOSED
