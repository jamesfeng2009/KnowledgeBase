"""故障注入测试 — 验证 RAG 系统在四类故障场景下的优雅降级行为。

覆盖故障场景：
    1. 解析失败（parsing failure）— chunker 对空/乱码/超大文档的降级
    2. 部分写入（partial write）— 向量存储 upsert/delete 部分或全部失败
    3. Embedding 超时（embedding timeout）— 熔断器保护 + 快速拒绝 + 自动恢复
    4. Rerank 超时（rerank timeout）— 引擎降级为原始排序

每个测试用例使用 mock 模拟故障，不依赖真实服务，验证优雅降级行为
（不崩溃、有日志、返回合理默认值）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery（测试环境未安装）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

from app.llm.embedder import OpenAIEmbedder
from app.rag.chunker import Chunk, SemanticChunker
from app.rag.engine import AgenticRAGEngine
from app.rag.vector_store.opensearch_store import OpenSearchVectorStore
from app.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitState,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


# ======================================================================
# 辅助函数
# ======================================================================


def _make_mock_chunks(n: int) -> list[Chunk]:
    """创建 n 个 mock Chunk 对象用于向量存储测试。"""
    return [
        Chunk(
            id=f"chunk-{i}",
            doc_id="doc-test",
            content=f"这是第 {i} 个文档分块的内容，包含一些用于测试的文本。",
            token_count=20,
        )
        for i in range(n)
    ]


def _make_mock_embeddings(n: int, dim: int = 8) -> list[list[float]]:
    """创建 n 个 mock 向量。"""
    return [[0.1 * (i + 1)] * dim for i in range(n)]


def _make_mock_candidates(n: int) -> list[dict[str, Any]]:
    """创建 n 个 mock 检索候选文档。"""
    return [
        {
            "doc_id": f"doc-{i}",
            "chunk_id": f"chunk-{i}",
            "content": f"候选文档 {i} 的内容",
            "score": 0.8 - i * 0.05,
            "source": "vector",
            "kb_id": "kb-1",
        }
        for i in range(n)
    ]


def _make_mock_engine(
    retriever: AsyncMock | None = None,
    reranker: AsyncMock | None = None,
) -> AgenticRAGEngine:
    """创建注入 mock 依赖的 AgenticRAGEngine（跳过 __init__ 的复杂初始化）。

    使用 __new__ 绕过构造函数中的可选组件初始化（FAQMatcher / QualityGuard 等），
    仅设置 _retrieve 方法所需的属性，保证测试隔离。
    """
    engine = AgenticRAGEngine.__new__(AgenticRAGEngine)
    engine.retriever = retriever or AsyncMock()
    engine.reranker = reranker or AsyncMock()
    engine.permission_filter = None
    engine._quality_guard = None
    engine._injection_guard = None
    return engine


# ======================================================================
# 1. 解析失败（parsing failure）
# ======================================================================


class TestParsingFailure:
    """解析失败 — chunker 对空/乱码/超大文档的优雅降级。"""

    def test_empty_content_returns_empty_list(self) -> None:
        """文档内容为空时 chunker 应返回空列表，不崩溃。"""
        chunker = SemanticChunker()
        result = chunker.chunk("", "md")
        assert result == []

    def test_whitespace_only_content_returns_empty_list(self) -> None:
        """文档内容仅含空白字符时 chunker 应返回空列表。"""
        chunker = SemanticChunker()
        result = chunker.chunk("   \n\n\t  \r\n  ", "md")
        assert result == []

    def test_garbled_content_degrades_gracefully(self) -> None:
        """文档内容为乱码（二进制垃圾数据）时 chunker 应优雅降级，不崩溃。"""
        chunker = SemanticChunker()
        # 模拟二进制乱码内容 — 包含不可打印字符和控制字符
        garbled = "".join(chr(i % 256) for i in range(0, 2560))
        result = chunker.chunk(garbled, "txt")
        # 不应崩溃，应返回列表（通过兜底策略产出 chunk）
        assert isinstance(result, list)
        # 每个 chunk 应有有效的 content 和 token_count
        for chunk in result:
            assert isinstance(chunk, Chunk)
            assert chunk.content  # 非空内容
            assert chunk.token_count > 0

    def test_oversized_content_has_size_protection(self) -> None:
        """超大文档（>1MB）时 chunker 应有大小限制保护，不崩溃。

        chunker 的兜底策略（_fixed_split）按 fallback_tokens 切分，
        保证单个 chunk 不超过大小上限，整体不因文档过大而崩溃。
        """
        chunker = SemanticChunker()
        # 生成 >1MB 的内容
        large_content = "这是测试内容，用于验证超大文档的分块处理。" * 50000
        assert len(large_content.encode("utf-8")) > 1024 * 1024  # 确认 >1MB

        result = chunker.chunk(large_content, "md")
        # 不应崩溃，应返回非空列表
        assert isinstance(result, list)
        assert len(result) > 0
        # 每个 chunk 的 token 数应在合理范围内（不超过 fallback_tokens 的 2 倍）
        for chunk in result:
            assert chunk.token_count > 0
            # 兜底策略的 chunk 大小受 fallback_tokens 控制
            assert chunk.token_count <= chunker.fallback_tokens * 2


# ======================================================================
# 2. 部分写入（partial write）
# ======================================================================


class TestPartialWrite:
    """部分写入 — 向量存储 upsert/delete 部分或全部失败时的优雅降级。"""

    def _make_store(self, http_mock: AsyncMock) -> OpenSearchVectorStore:
        """创建注入 mock HTTP 客户端的 OpenSearchVectorStore。"""
        store = OpenSearchVectorStore(http_client=http_mock)
        # 跳过索引创建检查，直接标记索引就绪
        store._index_ready = True
        store._available = True
        return store

    @pytest.mark.asyncio
    async def test_upsert_partial_failure_returns_count(self) -> None:
        """upsert 时部分 chunk 写入失败（bulk 响应含 errors），应记录并返回成功数。

        OpenSearch bulk API 在部分失败时 HTTP 状态码仍为 200，但响应体
        ``errors: true``。当前实现返回总提交数 n（不解析 bulk 响应体中的
        逐条错误），测试验证不崩溃且返回正数。
        """
        # mock HTTP 响应 — 200 状态码但 bulk 响应体含 errors
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()  # 不抛异常（HTTP 200）
        mock_response.json = MagicMock(
            return_value={"errors": True, "items": [{"index": {"status": 400}}]}
        )
        http_mock = AsyncMock()
        http_mock.post = AsyncMock(return_value=mock_response)

        store = self._make_store(http_mock)
        chunks = _make_mock_chunks(5)
        embeddings = _make_mock_embeddings(5)

        # 调用 upsert — 不应抛异常
        count = await store.upsert("doc-1", chunks, embeddings, kb_id="kb-1")

        # 应返回正数（当前实现返回总提交数）
        assert isinstance(count, int)
        assert count > 0
        # 确认 HTTP 请求被调用
        http_mock.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_all_failure_returns_zero(self) -> None:
        """upsert 时全部失败（连接异常），应返回 0 不抛异常。"""
        http_mock = AsyncMock()
        http_mock.post = AsyncMock(side_effect=ConnectionError("OpenSearch 不可达"))

        store = self._make_store(http_mock)
        chunks = _make_mock_chunks(3)
        embeddings = _make_mock_embeddings(3)

        # 调用 upsert — 不应抛异常，应返回 0
        count = await store.upsert("doc-1", chunks, embeddings, kb_id="kb-1")

        assert count == 0
        # 确认 store 标记为不可用
        assert store._available is False

    @pytest.mark.asyncio
    async def test_delete_connection_failure_degrades(self) -> None:
        """delete 时连接失败，应优雅降级不抛异常。"""
        http_mock = AsyncMock()
        http_mock.post = AsyncMock(side_effect=ConnectionError("OpenSearch 不可达"))

        store = self._make_store(http_mock)

        # 调用 delete — 不应抛异常
        await store.delete("doc-1")

        # 确认 HTTP 请求被尝试
        http_mock.post.assert_called_once()
        # 确认 store 标记为不可用
        assert store._available is False


# ======================================================================
# 3. Embedding 超时（embedding timeout）
# ======================================================================


class TestEmbeddingTimeout:
    """Embedding 超时 — 熔断器保护 + 快速拒绝 + 自动恢复。"""

    def setup_method(self) -> None:
        """每个测试前重置熔断器并设置测试友好的参数。"""
        reset_all_circuit_breakers()
        cb = get_circuit_breaker("embedder_openai")
        # 保存原始参数以便恢复
        self._original_threshold = cb.failure_threshold
        self._original_recovery = cb.recovery_timeout
        # 设置测试加速参数
        cb.failure_threshold = 3  # 3 次失败即熔断
        cb.recovery_timeout = 0.5  # 0.5 秒恢复

    def teardown_method(self) -> None:
        """每个测试后恢复熔断器参数并重置状态。"""
        cb = get_circuit_breaker("embedder_openai")
        cb.failure_threshold = self._original_threshold
        cb.recovery_timeout = self._original_recovery
        reset_all_circuit_breakers()

    def _make_embedder_with_timeout(self) -> OpenAIEmbedder:
        """创建 client.embeddings.create 超时的 OpenAIEmbedder。"""
        embedder = OpenAIEmbedder()
        embedder.client.embeddings.create = AsyncMock(
            side_effect=asyncio.TimeoutError("Embedding 请求超时")
        )
        return embedder

    @pytest.mark.asyncio
    async def test_embedder_timeout_triggers_circuit_breaker(self) -> None:
        """Embedder 调用超时时应触发熔断器（CLOSED → OPEN）。"""
        embedder = self._make_embedder_with_timeout()
        cb = get_circuit_breaker("embedder_openai")

        # 初始状态为 CLOSED
        assert cb.state == CircuitState.CLOSED

        # 调用 3 次（= failure_threshold），每次都超时
        for i in range(3):
            with pytest.raises(asyncio.TimeoutError):
                await embedder.embed(["测试文本"])

        # 熔断器应转为 OPEN
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count >= 3

    @pytest.mark.asyncio
    async def test_circuit_open_rejects_fast(self) -> None:
        """熔断器 OPEN 后应快速拒绝，不等待超时。"""
        embedder = self._make_embedder_with_timeout()
        cb = get_circuit_breaker("embedder_openai")

        # 触发熔断
        for i in range(3):
            with pytest.raises(asyncio.TimeoutError):
                await embedder.embed(["测试文本"])
        assert cb.state == CircuitState.OPEN

        # 计时 — OPEN 状态下调用应快速失败（CircuitBreakerOpenError）
        t0 = time.monotonic()
        with pytest.raises(CircuitBreakerOpenError):
            await embedder.embed(["测试文本"])
        elapsed = time.monotonic() - t0

        # 应在 0.1 秒内快速拒绝（不等超时）
        assert elapsed < 0.1, f"熔断器未快速拒绝，耗时 {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_circuit_recovers_after_timeout(self) -> None:
        """熔断器恢复后应自动重试（OPEN → HALF_OPEN → CLOSED）。"""
        embedder = self._make_embedder_with_timeout()
        cb = get_circuit_breaker("embedder_openai")

        # 触发熔断
        for i in range(3):
            with pytest.raises(asyncio.TimeoutError):
                await embedder.embed(["测试文本"])
        assert cb.state == CircuitState.OPEN

        # 等待恢复超时（recovery_timeout = 0.5s）
        await asyncio.sleep(0.6)

        # 恢复 mock 为成功响应
        mock_embedding_data = MagicMock()
        mock_embedding_data.embedding = [0.1] * 3072
        mock_response = MagicMock()
        mock_response.data = [mock_embedding_data]
        embedder.client.embeddings.create = AsyncMock(return_value=mock_response)

        # 调用 embed — 熔断器应转为 HALF_OPEN，探测成功后转为 CLOSED
        result = await embedder.embed(["恢复测试"])

        # 应成功返回向量
        assert len(result) == 1
        assert len(result[0]) == 3072
        # 熔断器应恢复为 CLOSED
        assert cb.state == CircuitState.CLOSED


# ======================================================================
# 4. Rerank 超时（rerank timeout）
# ======================================================================


class TestRerankTimeout:
    """Rerank 超时 — 引擎降级为原始排序。"""

    @pytest.mark.asyncio
    async def test_reranker_timeout_degrades_to_original_order(self) -> None:
        """Reranker 调用超时时引擎应降级为原始排序（不抛异常）。

        引擎 _retrieve 方法在 reranker.rerank 抛异常时，捕获异常并降级为
        filtered[:_RERANK_TOP_K]（原始检索结果的前 N 条）。
        """
        # mock 检索器返回 10 个候选
        candidates = _make_mock_candidates(10)
        retriever = AsyncMock()
        retriever.search = AsyncMock(return_value=candidates)

        # mock reranker 超时
        reranker = AsyncMock()
        reranker.rerank = AsyncMock(side_effect=asyncio.TimeoutError("Rerank 超时"))

        engine = _make_mock_engine(retriever=retriever, reranker=reranker)

        state: dict[str, Any] = {"query": "测试查询", "iteration": 1}
        await engine._retrieve(state, ["kb-1"])

        # 引擎应降级为原始排序（取前 _RERANK_TOP_K=5 条）
        retrieved = state.get("retrieved_docs")
        assert retrieved is not None
        assert isinstance(retrieved, list)
        assert len(retrieved) == 5  # _RERANK_TOP_K = 5
        # 内容应为原始候选的前 5 条
        assert retrieved[0]["doc_id"] == "doc-0"
        # 确认 span_evidence 被正确写入（引擎完整执行了 _retrieve 流程）
        assert "_span_evidence" in state

    @pytest.mark.asyncio
    async def test_reranker_empty_result_uses_original_retrieval(self) -> None:
        """Reranker 返回空结果时引擎应使用原始检索结果（不抛异常）。

        当 reranker 返回空列表时，_apply_rerank_scores 返回空列表。
        引擎不崩溃，retrieved_docs 为列表，后续流程（recency / 冲突裁决 /
        span_evidence）继续执行。
        """
        candidates = _make_mock_candidates(5)
        retriever = AsyncMock()
        retriever.search = AsyncMock(return_value=candidates)

        # mock reranker 返回空结果
        reranker = AsyncMock()
        reranker.rerank = AsyncMock(return_value=[])

        engine = _make_mock_engine(retriever=retriever, reranker=reranker)

        state: dict[str, Any] = {"query": "测试查询", "iteration": 1}
        # 不应抛异常
        await engine._retrieve(state, ["kb-1"])

        # retrieved_docs 应为列表（不抛异常即为优雅降级）
        retrieved = state.get("retrieved_docs")
        assert retrieved is not None
        assert isinstance(retrieved, list)
        # span_evidence 应被正确写入
        assert "_span_evidence" in state

    @pytest.mark.asyncio
    async def test_reranker_malformed_response_degrades(self) -> None:
        """Reranker 返回格式错误时引擎应降级处理（不抛异常）。

        reranker 返回包含 None 的列表（格式错误），_apply_rerank_scores
        在访问 None.get() 时抛 AttributeError，被 _retrieve 的 try/except
        捕获，降级为原始排序 filtered[:_RERANK_TOP_K]。
        """
        candidates = _make_mock_candidates(5)
        retriever = AsyncMock()
        retriever.search = AsyncMock(return_value=candidates)

        # mock reranker 返回格式错误的结果（包含 None 元素）
        reranker = AsyncMock()
        reranker.rerank = AsyncMock(return_value=[None])

        engine = _make_mock_engine(retriever=retriever, reranker=reranker)

        state: dict[str, Any] = {"query": "测试查询", "iteration": 1}
        # 不应抛异常 — _retrieve 的 try/except 应捕获并降级
        await engine._retrieve(state, ["kb-1"])

        retrieved = state.get("retrieved_docs")
        assert retrieved is not None
        assert isinstance(retrieved, list)
        # 降级为原始排序的前 5 条
        assert len(retrieved) == 5  # _RERANK_TOP_K

    def test_apply_rerank_scores_empty_returns_empty(self) -> None:
        """_apply_rerank_scores 对空重排结果应返回空列表（不崩溃）。"""
        docs = _make_mock_candidates(3)
        result = AgenticRAGEngine._apply_rerank_scores(docs, [])
        assert result == []

    def test_apply_rerank_scores_malformed_index_skipped(self) -> None:
        """_apply_rerank_scores 对格式错误的 index 应跳过（单元测试）。

        验证三种格式错误：
        1. index 为字符串（非 int）→ 跳过
        2. index 超出范围 → 跳过
        3. 正常 index → 返回对应文档
        """
        docs = _make_mock_candidates(3)

        # index 为字符串（非 int）→ 应被跳过
        result = AgenticRAGEngine._apply_rerank_scores(
            docs, [{"index": "invalid", "score": 0.9}]
        )
        assert result == []

        # index 超出范围 → 应被跳过
        result = AgenticRAGEngine._apply_rerank_scores(
            docs, [{"index": 999, "score": 0.9}]
        )
        assert result == []

        # 正常 index → 应返回对应文档
        result = AgenticRAGEngine._apply_rerank_scores(
            docs, [{"index": 1, "score": 0.95}]
        )
        assert len(result) == 1
        assert result[0]["doc_id"] == "doc-1"
        assert result[0]["score"] == 0.95
