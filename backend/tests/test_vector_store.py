"""向量存储适配器测试 — 验证 VectorStoreBase 抽象层、两个后端实现、工厂、集成。

覆盖：
- VectorStoreBase：抽象类不可实例化、_format_result 工具方法
- OpenSearchVectorStore：search / upsert / delete / health_check / _ensure_index
- MilvusVectorStore：search / upsert / delete / health_check
- Factory：get_vector_store() 后端选择、缓存、无效后端
- HybridRetriever 集成：通过 VectorStoreBase 执行向量检索
- document_tasks 集成：_build_vector_index 通过适配器写入
- 向后兼容：_build_milvus_index 委托到 _build_vector_index
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

if "opensearchpy" not in sys.modules:
    sys.modules["opensearchpy"] = MagicMock()

if "pymilvus" not in sys.modules:
    sys.modules["pymilvus"] = MagicMock()

from app.rag.chunker import Chunk
from app.rag.vector_store.base import VectorStoreBase
from app.rag.vector_store.factory import (
    _DEFAULT_BACKEND,
    get_supported_backends,
    get_vector_store,
    reset_vector_store_cache,
)
from app.rag.vector_store.milvus_store import MilvusVectorStore
from app.rag.vector_store.opensearch_store import OpenSearchVectorStore
from app.utils.circuit_breaker import reset_all_circuit_breakers


@pytest.fixture(autouse=True)
def _reset_breakers():
    """每个测试前后重置全局熔断器状态，避免跨文件状态污染。"""
    reset_all_circuit_breakers()
    yield
    reset_all_circuit_breakers()


# ======================================================================
# 辅助工具
# ======================================================================


def _make_chunks(count: int = 2) -> list[Chunk]:
    """生成测试用 Chunk 对象列表。"""
    return [
        Chunk(
            id=f"chunk-{i}",
            doc_id="doc-001",
            content=f"这是第{i+1}个分块的向量测试内容。",
            parent_id=None if i == 0 else "chunk-0",
            start_pos=i * 100,
            end_pos=(i + 1) * 100,
            token_count=50,
            title_path=f"标题 > 子标题{i+1}" if i > 0 else "标题",
            content_type="tutorial",
            chunk_strategy="structural",
        )
        for i in range(count)
    ]


def _make_embeddings(count: int = 2, dim: int = 1024) -> list[list[float]]:
    """生成测试用向量列表。"""
    return [[0.1 * (i + 1)] * dim for i in range(count)]


class MockResponse:
    """模拟 httpx.Response。"""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://mock"),
                response=httpx.Response(self.status_code),
            )


def _make_mock_http(
    post_return: MockResponse | dict[str, MockResponse] | None = None,
    get_return: MockResponse | None = None,
    head_return: MockResponse | None = None,
    put_return: MockResponse | None = None,
) -> MagicMock:
    """创建模拟 httpx.AsyncClient。

    post_return 可以是单个 MockResponse（所有 POST 返回相同）或
    dict[str, MockResponse]（按 URL 后缀匹配不同响应）。
    """
    client = MagicMock()
    client.aclose = AsyncMock()

    async def _post(url: str, **kwargs: Any) -> MockResponse:
        if isinstance(post_return, dict):
            for suffix, resp in post_return.items():
                if suffix in url:
                    return resp
            return MockResponse(status_code=404)
        return post_return or MockResponse()

    async def _get(url: str, **kwargs: Any) -> MockResponse:
        return get_return or MockResponse()

    async def _head(url: str, **kwargs: Any) -> MockResponse:
        return head_return or MockResponse(status_code=404)

    async def _put(url: str, **kwargs: Any) -> MockResponse:
        return put_return or MockResponse()

    client.post = _post
    client.get = _get
    client.head = _head
    client.put = _put
    return client


# ======================================================================
# VectorStoreBase 抽象类测试
# ======================================================================


class TestVectorStoreBase:
    """VectorStoreBase 抽象接口测试。"""

    def test_cannot_instantiate_abstract_class(self) -> None:
        """VectorStoreBase 是抽象类，不能直接实例化。"""
        with pytest.raises(TypeError):
            VectorStoreBase()  # type: ignore[abstract]

    def test_format_result_returns_correct_dict(self) -> None:
        """_format_result 返回统一格式字典。"""
        result = VectorStoreBase._format_result(
            doc_id="doc-001",
            chunk_id="chunk-0",
            content="测试内容",
            score=0.95,
            kb_id="kb-001",
            title="标题路径",
        )
        assert result["doc_id"] == "doc-001"
        assert result["chunk_id"] == "chunk-0"
        assert result["content"] == "测试内容"
        assert result["score"] == 0.95
        assert result["source"] == "vector"
        assert result["kb_id"] == "kb-001"
        assert result["title"] == "标题路径"

    def test_format_result_with_none_values(self) -> None:
        """_format_result 处理 None 值。"""
        result = VectorStoreBase._format_result(
            doc_id="doc-001",
            chunk_id="chunk-0",
            content="内容",
            score=0.5,
        )
        assert result["kb_id"] is None
        assert result["title"] is None

    def test_default_dimension(self) -> None:
        """默认向量维度为 1024。"""
        # 通过子类实例检查
        store = OpenSearchVectorStore(
            http_client=_make_mock_http()
        )
        assert store.dimension == 1024


# ======================================================================
# OpenSearchVectorStore 测试
# ======================================================================


class TestOpenSearchVectorStore:
    """OpenSearchVectorStore k-NN 后端测试。"""

    def test_inherits_vector_store_base(self) -> None:
        """OpenSearchVectorStore 继承 VectorStoreBase。"""
        store = OpenSearchVectorStore(http_client=_make_mock_http())
        assert isinstance(store, VectorStoreBase)

    @pytest.mark.asyncio
    async def test_search_returns_results(self) -> None:
        """search 返回 k-NN 检索结果。"""
        search_response = MockResponse(
            json_data={
                "hits": {
                    "hits": [
                        {
                            "_id": "chunk-0",
                            "_score": 0.95,
                            "_source": {
                                "doc_id": "doc-001",
                                "chunk_id": "chunk-0",
                                "content": "向量检索内容",
                                "kb_id": "kb-001",
                                "title_path": "标题",
                            },
                        },
                        {
                            "_id": "chunk-1",
                            "_score": 0.85,
                            "_source": {
                                "doc_id": "doc-001",
                                "chunk_id": "chunk-1",
                                "content": "第二个结果",
                                "kb_id": "kb-001",
                                "title_path": "标题 > 子标题",
                            },
                        },
                    ]
                }
            }
        )
        client = _make_mock_http(post_return=search_response)
        store = OpenSearchVectorStore(http_client=client)

        results = await store.search(
            query_vec=[0.1] * 1024, top_k=10
        )
        assert len(results) == 2
        assert results[0]["doc_id"] == "doc-001"
        assert results[0]["chunk_id"] == "chunk-0"
        assert results[0]["score"] == 0.95
        assert results[0]["source"] == "vector"
        assert results[0]["title"] == "标题"

    @pytest.mark.asyncio
    async def test_search_with_kb_ids_filter(self) -> None:
        """search 支持 kb_ids 过滤。"""
        search_response = MockResponse(
            json_data={"hits": {"hits": []}}
        )
        client = _make_mock_http(post_return=search_response)
        store = OpenSearchVectorStore(http_client=client)

        results = await store.search(
            query_vec=[0.1] * 1024,
            kb_ids=["kb-001", "kb-002"],
            top_k=5,
        )
        assert results == []
        # 验证请求被发送
        assert store._available is True

    @pytest.mark.asyncio
    async def test_search_raises_on_error(self) -> None:
        """服务不可用时异常向上传播，触发熔断器记录失败（由调用方降级为空列表）。"""
        error_response = MockResponse(status_code=500)
        client = _make_mock_http(post_return=error_response)
        store = OpenSearchVectorStore(http_client=client)

        with pytest.raises(httpx.HTTPStatusError):
            await store.search(query_vec=[0.1] * 1024)

    @pytest.mark.asyncio
    async def test_search_short_circuits_when_unavailable(self) -> None:
        """_available=False（由写入/健康检查路径标记）时 search 快速返回空列表。"""
        ok_response = MockResponse(json_data={"hits": {"hits": []}})
        client = _make_mock_http(post_return=ok_response)
        store = OpenSearchVectorStore(http_client=client)
        store._available = False

        results = await store.search(query_vec=[0.1] * 1024)
        assert results == []

    @pytest.mark.asyncio
    async def test_upsert_writes_vectors(self) -> None:
        """upsert 批量写入向量数据。"""
        # HEAD 请求返回 200（索引已存在）
        head_resp = MockResponse(status_code=200)
        bulk_resp = MockResponse(json_data={"errors": False})
        client = _make_mock_http(
            post_return=bulk_resp,
            head_return=head_resp,
        )
        store = OpenSearchVectorStore(http_client=client)

        chunks = _make_chunks(2)
        embeddings = _make_embeddings(2)
        count = await store.upsert("doc-001", chunks, embeddings)

        assert count == 2
        assert store._available is True

    @pytest.mark.asyncio
    async def test_upsert_returns_zero_for_empty_embeddings(self) -> None:
        """空 embeddings 返回 0。"""
        store = OpenSearchVectorStore(http_client=_make_mock_http())
        count = await store.upsert("doc-001", [], [])
        assert count == 0

    @pytest.mark.asyncio
    async def test_upsert_creates_index_if_not_exists(self) -> None:
        """索引不存在时自动创建。"""
        # HEAD 返回 404（不存在），PUT 返回 200（创建成功），POST bulk 返回成功
        head_resp = MockResponse(status_code=404)
        put_resp = MockResponse(status_code=200)
        bulk_resp = MockResponse(json_data={"errors": False})
        client = _make_mock_http(
            post_return=bulk_resp,
            head_return=head_resp,
            put_return=put_resp,
        )
        store = OpenSearchVectorStore(http_client=client)

        chunks = _make_chunks(1)
        embeddings = _make_embeddings(1)
        count = await store.upsert("doc-001", chunks, embeddings)

        assert count == 1
        assert store._available is True

    @pytest.mark.asyncio
    async def test_delete_removes_by_doc_id(self) -> None:
        """delete 按 doc_id 删除向量。"""
        delete_resp = MockResponse(json_data={"deleted": 5})
        client = _make_mock_http(post_return=delete_resp)
        store = OpenSearchVectorStore(http_client=client)

        await store.delete("doc-001")
        assert store._available is True

    @pytest.mark.asyncio
    async def test_health_check_returns_true_when_available(self) -> None:
        """服务可用时 health_check 返回 True。"""
        health_resp = MockResponse(
            json_data={"status": "green"}
        )
        client = _make_mock_http(get_return=health_resp)
        store = OpenSearchVectorStore(http_client=client)

        result = await store.health_check()
        assert result is True
        assert store._available is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_error(self) -> None:
        """服务不可用时 health_check 返回 False。"""
        client = MagicMock()
        client.aclose = AsyncMock()
        client.get = AsyncMock(side_effect=Exception("connection refused"))
        store = OpenSearchVectorStore(http_client=client)

        result = await store.health_check()
        assert result is False
        assert store._available is False


# ======================================================================
# MilvusVectorStore 测试
# ======================================================================


class TestMilvusVectorStore:
    """MilvusVectorStore 后端测试。"""

    def test_inherits_vector_store_base(self) -> None:
        """MilvusVectorStore 继承 VectorStoreBase。"""
        store = MilvusVectorStore(http_client=_make_mock_http())
        assert isinstance(store, VectorStoreBase)

    @pytest.mark.asyncio
    async def test_search_returns_results(self) -> None:
        """search 返回 Milvus 向量检索结果。"""
        search_response = MockResponse(
            json_data={
                "data": [
                    {
                        "distance": 0.92,
                        "doc_id": "doc-001",
                        "chunk_id": "chunk-0",
                        "content": "Milvus 向量内容",
                        "kb_id": "kb-001",
                        "title_path": "标题",
                    },
                ]
            }
        )
        client = _make_mock_http(post_return=search_response)
        store = MilvusVectorStore(http_client=client)

        results = await store.search(
            query_vec=[0.1] * 1024, top_k=10
        )
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc-001"
        assert results[0]["chunk_id"] == "chunk-0"
        assert results[0]["score"] == 0.92
        assert results[0]["source"] == "vector"
        assert results[0]["title"] == "标题"

    @pytest.mark.asyncio
    async def test_search_with_kb_ids_filter(self) -> None:
        """search 支持 kb_ids 过滤。"""
        search_response = MockResponse(json_data={"data": []})
        client = _make_mock_http(post_return=search_response)
        store = MilvusVectorStore(http_client=client)

        results = await store.search(
            query_vec=[0.1] * 1024,
            kb_ids=["kb-001"],
            top_k=5,
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_search_raises_on_error(self) -> None:
        """服务不可用时异常向上传播，触发熔断器记录失败（由调用方降级为空列表）。"""
        error_response = MockResponse(status_code=500)
        client = _make_mock_http(post_return=error_response)
        store = MilvusVectorStore(http_client=client)

        with pytest.raises(httpx.HTTPStatusError):
            await store.search(query_vec=[0.1] * 1024)

    @pytest.mark.asyncio
    async def test_search_short_circuits_when_unavailable(self) -> None:
        """_available=False（由写入/健康检查路径标记）时 search 快速返回空列表。"""
        ok_response = MockResponse(json_data={"data": []})
        client = _make_mock_http(post_return=ok_response)
        store = MilvusVectorStore(http_client=client)
        store._available = False

        results = await store.search(query_vec=[0.1] * 1024)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_parses_list_format(self) -> None:
        """search 解析 list 格式的返回数据。"""
        list_response = MockResponse(
            json_data=[
                {
                    "distance": 0.88,
                    "doc_id": "doc-002",
                    "chunk_id": "chunk-5",
                    "content": "列表格式结果",
                    "kb_id": "kb-002",
                    "title_path": "标题2",
                }
            ]
        )
        client = _make_mock_http(post_return=list_response)
        store = MilvusVectorStore(http_client=client)

        results = await store.search(query_vec=[0.1] * 1024, top_k=5)
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc-002"
        assert results[0]["score"] == 0.88

    @pytest.mark.asyncio
    async def test_upsert_writes_vectors(self) -> None:
        """upsert 批量写入向量数据到 Milvus。"""
        upsert_resp = MockResponse(
            json_data={"upsertCnt": 2}
        )
        client = _make_mock_http(post_return=upsert_resp)
        store = MilvusVectorStore(http_client=client)

        chunks = _make_chunks(2)
        embeddings = _make_embeddings(2)
        count = await store.upsert("doc-001", chunks, embeddings)

        assert count == 2
        assert store._available is True

    @pytest.mark.asyncio
    async def test_upsert_returns_zero_for_empty_embeddings(self) -> None:
        """空 embeddings 返回 0。"""
        store = MilvusVectorStore(http_client=_make_mock_http())
        count = await store.upsert("doc-001", [], [])
        assert count == 0

    @pytest.mark.asyncio
    async def test_upsert_returns_zero_on_error(self) -> None:
        """服务不可用时 upsert 返回 0。"""
        error_response = MockResponse(status_code=500)
        client = _make_mock_http(post_return=error_response)
        store = MilvusVectorStore(http_client=client)

        chunks = _make_chunks(1)
        embeddings = _make_embeddings(1)
        count = await store.upsert("doc-001", chunks, embeddings)

        assert count == 0
        assert store._available is False

    @pytest.mark.asyncio
    async def test_delete_removes_by_doc_id(self) -> None:
        """delete 按 doc_id 删除向量。"""
        delete_resp = MockResponse(json_data={"deleteCnt": 3})
        client = _make_mock_http(post_return=delete_resp)
        store = MilvusVectorStore(http_client=client)

        await store.delete("doc-001")
        assert store._available is True

    @pytest.mark.asyncio
    async def test_health_check_returns_true_when_available(self) -> None:
        """服务可用时 health_check 返回 True。"""
        health_resp = MockResponse(
            json_data={"data": {"collection_names": []}}
        )
        client = _make_mock_http(post_return=health_resp)
        store = MilvusVectorStore(http_client=client)

        result = await store.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_error(self) -> None:
        """服务不可用时 health_check 返回 False。"""
        client = MagicMock()
        client.aclose = AsyncMock()
        client.post = AsyncMock(side_effect=Exception("connection refused"))
        store = MilvusVectorStore(http_client=client)

        result = await store.health_check()
        assert result is False


# ======================================================================
# Factory 工厂测试
# ======================================================================


class TestVectorStoreFactory:
    """get_vector_store() 工厂函数测试。"""

    def setup_method(self) -> None:
        """每个测试前重置工厂缓存。"""
        reset_vector_store_cache()

    def teardown_method(self) -> None:
        """每个测试后重置工厂缓存。"""
        reset_vector_store_cache()

    def test_get_supported_backends(self) -> None:
        """get_supported_backends 返回支持的后端列表。"""
        backends = get_supported_backends()
        assert "os_knn" in backends
        assert "milvus" in backends

    def test_default_backend_is_os_knn(self) -> None:
        """默认后端为 os_knn。"""
        assert _DEFAULT_BACKEND == "os_knn"

    def test_factory_returns_opensearch_by_default(self) -> None:
        """默认配置返回 OpenSearchVectorStore。"""
        with patch("app.rag.vector_store.factory.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(VECTOR_STORE="os_knn")
            store = get_vector_store()
            assert isinstance(store, OpenSearchVectorStore)

    def test_factory_returns_milvus_when_configured(self) -> None:
        """VECTOR_STORE=milvus 时返回 MilvusVectorStore。"""
        with patch("app.rag.vector_store.factory.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(VECTOR_STORE="milvus")
            store = get_vector_store()
            assert isinstance(store, MilvusVectorStore)

    def test_factory_caches_instance(self) -> None:
        """工厂缓存实例 — 多次调用返回同一对象。"""
        with patch("app.rag.vector_store.factory.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(VECTOR_STORE="os_knn")
            store1 = get_vector_store()
            store2 = get_vector_store()
            assert store1 is store2

    def test_factory_raises_on_invalid_backend(self) -> None:
        """不支持的向量存储后端抛出 ValueError。"""
        with patch("app.rag.vector_store.factory.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(VECTOR_STORE="redis")
            with pytest.raises(ValueError, match="不支持的向量存储后端"):
                get_vector_store()

    def test_factory_falls_back_on_empty_config(self) -> None:
        """VECTOR_STORE 为空时回退到默认后端。"""
        with patch("app.rag.vector_store.factory.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(VECTOR_STORE="")
            store = get_vector_store()
            assert isinstance(store, OpenSearchVectorStore)

    def test_reset_cache_allows_backend_switch(self) -> None:
        """重置缓存后可切换后端。"""
        with patch("app.rag.vector_store.factory.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(VECTOR_STORE="os_knn")
            store1 = get_vector_store()
            assert isinstance(store1, OpenSearchVectorStore)

            reset_vector_store_cache()

            mock_settings.return_value = MagicMock(VECTOR_STORE="milvus")
            store2 = get_vector_store()
            assert isinstance(store2, MilvusVectorStore)


# ======================================================================
# HybridRetriever 集成测试 — 通过 VectorStoreBase 检索
# ======================================================================


class TestRetrieverWithVectorStore:
    """HybridRetriever 通过 VectorStoreBase 适配器执行向量检索。"""

    @pytest.mark.asyncio
    async def test_retriever_uses_injected_vector_store(self) -> None:
        """HybridRetriever 使用注入的 vector_store 执行向量检索。"""
        from app.rag.retriever import HybridRetriever

        mock_vector_store = MagicMock(spec=VectorStoreBase)
        mock_vector_store.search = AsyncMock(
            return_value=[
                {
                    "doc_id": "doc-001",
                    "chunk_id": "chunk-0",
                    "content": "向量结果",
                    "score": 0.95,
                    "source": "vector",
                    "kb_id": "kb-001",
                    "title": "标题",
                }
            ]
        )

        mock_embedder = MagicMock()
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1024])

        # Mock HTTP for fulltext search (returns empty)
        mock_http = MagicMock()
        mock_http.aclose = AsyncMock()
        mock_http.post = AsyncMock(
            return_value=MockResponse(json_data={"hits": {"hits": []}})
        )

        retriever = HybridRetriever(
            embedder=mock_embedder,
            http_client=mock_http,
            vector_store=mock_vector_store,
        )

        results = await retriever.search("测试查询", top_k=10)

        # 验证 vector_store.search 被调用
        mock_vector_store.search.assert_called_once()
        call_args = mock_vector_store.search.call_args
        assert call_args.kwargs["top_k"] == 10

        # 验证结果包含向量检索结果
        assert len(results) == 1
        assert results[0]["content"] == "向量结果"

    @pytest.mark.asyncio
    async def test_retriever_works_without_vector_store_injection(self) -> None:
        """未注入 vector_store 时，通过工厂获取（优雅降级）。"""
        from app.rag.retriever import HybridRetriever

        mock_store = MagicMock(spec=VectorStoreBase)
        mock_store.search = AsyncMock(return_value=[])

        mock_embedder = MagicMock()
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1024])

        mock_http = MagicMock()
        mock_http.aclose = AsyncMock()
        mock_http.post = AsyncMock(
            return_value=MockResponse(json_data={"hits": {"hits": []}})
        )

        with patch("app.rag.retriever.get_vector_store", return_value=mock_store):
            retriever = HybridRetriever(
                embedder=mock_embedder,
                http_client=mock_http,
            )
            results = await retriever.search("测试查询")

        mock_store.search.assert_called_once()
        assert results == []

    @pytest.mark.asyncio
    async def test_retriever_returns_empty_when_embedder_fails(self) -> None:
        """Embedder 不可用时向量检索返回空列表。"""
        from app.rag.retriever import HybridRetriever

        mock_store = MagicMock(spec=VectorStoreBase)
        mock_store.search = AsyncMock(return_value=[])

        mock_http = MagicMock()
        mock_http.aclose = AsyncMock()
        mock_http.post = AsyncMock(
            return_value=MockResponse(json_data={"hits": {"hits": []}})
        )

        # Embedder 返回 None
        with patch("app.rag.retriever.get_embedder", side_effect=Exception("no embedder")):
            retriever = HybridRetriever(
                http_client=mock_http,
                vector_store=mock_store,
            )
            results = await retriever.search("测试查询")

        # 向量检索未执行，但全文检索仍尝试
        mock_store.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_retriever_merges_vector_and_fulltext(self) -> None:
        """向量 + 全文结果合并去重。"""
        from app.rag.retriever import HybridRetriever

        mock_store = MagicMock(spec=VectorStoreBase)
        mock_store.search = AsyncMock(
            return_value=[
                {
                    "doc_id": "doc-001",
                    "chunk_id": "chunk-0",
                    "content": "向量内容",
                    "score": 0.9,
                    "source": "vector",
                    "kb_id": None,
                    "title": None,
                }
            ]
        )

        mock_embedder = MagicMock()
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1024])

        fulltext_response = MockResponse(
            json_data={
                "hits": {
                    "hits": [
                        {
                            "_id": "chunk-1",
                            "_score": 0.8,
                            "_source": {
                                "doc_id": "doc-002",
                                "chunk_id": "chunk-1",
                                "chunk_text": "全文内容",
                                "kb_id": "kb-001",
                            },
                        }
                    ]
                }
            }
        )
        mock_http = MagicMock()
        mock_http.aclose = AsyncMock()
        mock_http.post = AsyncMock(return_value=fulltext_response)

        retriever = HybridRetriever(
            embedder=mock_embedder,
            http_client=mock_http,
            vector_store=mock_store,
        )

        results = await retriever.search("测试", top_k=10)
        assert len(results) == 2
        chunk_ids = {r["chunk_id"] for r in results}
        assert "chunk-0" in chunk_ids
        assert "chunk-1" in chunk_ids

    @pytest.mark.asyncio
    async def test_retriever_empty_query_returns_empty(self) -> None:
        """空查询返回空列表。"""
        from app.rag.retriever import HybridRetriever

        mock_store = MagicMock(spec=VectorStoreBase)
        mock_store.search = AsyncMock(return_value=[])

        retriever = HybridRetriever(
            embedder=MagicMock(),
            http_client=_make_mock_http(),
            vector_store=mock_store,
        )

        results = await retriever.search("", top_k=10)
        assert results == []
        mock_store.search.assert_not_called()


# ======================================================================
# document_tasks _build_vector_index 集成测试
# ======================================================================


class TestDocumentTasksVectorIndex:
    """document_tasks._build_vector_index 通过适配器写入测试。"""

    @pytest.mark.asyncio
    async def test_build_vector_index_uses_adapter(self) -> None:
        """_build_vector_index 通过 VectorStoreBase.upsert 写入。"""
        from tasks.document_tasks import _build_vector_index

        mock_store = MagicMock(spec=VectorStoreBase)
        mock_store.upsert = AsyncMock(return_value=2)

        chunks = _make_chunks(2)
        embeddings = _make_embeddings(2)

        with patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            count = await _build_vector_index("doc-001", chunks, embeddings)

        assert count == 2
        mock_store.upsert.assert_called_once_with("doc-001", chunks, embeddings)

    @pytest.mark.asyncio
    async def test_build_vector_index_returns_zero_for_empty_embeddings(self) -> None:
        """空 embeddings 返回 0，不调用 upsert。"""
        from tasks.document_tasks import _build_vector_index

        mock_store = MagicMock(spec=VectorStoreBase)
        mock_store.upsert = AsyncMock(return_value=0)

        with patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            count = await _build_vector_index("doc-001", [], [])

        assert count == 0
        mock_store.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_build_milvus_index_backward_compatible(self) -> None:
        """_build_milvus_index 向后兼容 — 委托到 _build_vector_index。"""
        from tasks.document_tasks import _build_milvus_index

        mock_store = MagicMock(spec=VectorStoreBase)
        mock_store.upsert = AsyncMock(return_value=1)

        chunks = _make_chunks(1)
        embeddings = _make_embeddings(1)

        with patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            # 不应抛出异常
            await _build_milvus_index("doc-001", chunks, embeddings)

        mock_store.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_indexes_calls_vector_index(self) -> None:
        """_build_indexes 调用 _build_vector_index 而非直接调 Milvus。"""
        from tasks.document_tasks import _build_indexes

        chunks = _make_chunks(2)
        chunks_text = [c.content for c in chunks]
        embeddings = _make_embeddings(2)

        with patch(
            "tasks.document_tasks._build_opensearch_index",
            new_callable=AsyncMock,
        ) as mock_os, patch(
            "tasks.document_tasks._build_vector_index",
            new_callable=AsyncMock,
            return_value=2,
        ) as mock_vec:
            await _build_indexes("doc-001", chunks, chunks_text, embeddings)
            mock_os.assert_called_once_with("doc-001", chunks)
            mock_vec.assert_called_once_with("doc-001", chunks, embeddings)
