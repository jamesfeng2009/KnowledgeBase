"""HybridRetriever 层级过滤透传测试 — P0 wiki 层级改造。

验证 HybridRetriever.search 的 filters 参数正确透传给三路子检索：
    - _vector_search → store.search(filters=...)
    - _fulltext_search → OpenSearch payload bool.filter 子句
    - _cross_modal_search → cm_store.search(filters=...)
    - _graph_search 不接收 filters（图谱路不走层级过滤）

mock 策略：注入 AsyncMock 的 embedder / vector_store / http_client，
避免真实 OpenSearch / Milvus 依赖。
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery（测试环境未安装）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

# httpx_retry 已在 app/utils/retry.py 中改为延迟导入，无需 sys.modules mock；
# 之前模块级 mock 会泄漏到 test_retry.py 导致 MagicMock 无法被 await。

from app.rag.retriever import HybridRetriever


@pytest.fixture
def retriever_with_mocks() -> HybridRetriever:
    """构造注入 mock 依赖的 HybridRetriever。"""
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[[0.1] * 8])

    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])

    http_client = AsyncMock()
    # OpenSearch 返回空结果
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"hits": {"hits": []}})
    http_client.post = AsyncMock(return_value=mock_response)

    retriever = HybridRetriever(
        embedder=embedder,
        http_client=http_client,
        vector_store=vector_store,
    )
    return retriever


class TestVectorSearchFilters:
    """_vector_search 透传 filters 给 store.search。"""

    @pytest.mark.asyncio
    async def test_filters_passed_to_store_search(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        filters = {"series_id": "prod-a", "depth": 0}
        await retriever_with_mocks._vector_search(
            "查询", ["kb1"], 20, filters
        )
        retriever_with_mocks._vector_store.search.assert_called_once()
        call_kwargs = retriever_with_mocks._vector_store.search.call_args.kwargs
        assert call_kwargs.get("filters") == filters

    @pytest.mark.asyncio
    async def test_none_filters_passed_through(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """filters=None 也应透传（向后兼容，store 内部处理 None）。"""
        await retriever_with_mocks._vector_search("查询", ["kb1"], 20, None)
        call_kwargs = retriever_with_mocks._vector_store.search.call_args.kwargs
        assert call_kwargs.get("filters") is None


class TestFulltextSearchFilters:
    """_fulltext_search 将 filters 转为 OpenSearch bool.filter 子句。"""

    @pytest.mark.asyncio
    async def test_filters_become_filter_clauses(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        filters = {"series_id": "s1", "depth": 1}
        await retriever_with_mocks._fulltext_search("查询", ["kb1"], 20, filters)
        payload = retriever_with_mocks._http.post.call_args.kwargs.get("json", {})
        filter_clauses = payload.get("query", {}).get("bool", {}).get("filter", [])
        # 应包含 series_id term 和 depth term（以及 kb_id terms）
        assert {"term": {"series_id": "s1"}} in filter_clauses
        assert {"term": {"depth": 1}} in filter_clauses
        assert {"terms": {"kb_id": ["kb1"]}} in filter_clauses

    @pytest.mark.asyncio
    async def test_path_prefix_becomes_prefix_clause(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        await retriever_with_mocks._fulltext_search(
            "查询", None, 20, {"path_prefix": "产品/合规/"}
        )
        payload = retriever_with_mocks._http.post.call_args.kwargs.get("json", {})
        filter_clauses = payload.get("query", {}).get("bool", {}).get("filter", [])
        assert {"prefix": {"path": "产品/合规/"}} in filter_clauses

    @pytest.mark.asyncio
    async def test_no_filters_no_filter_array(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """filters=None 且无 kb_ids 时，不应添加 filter 数组。"""
        await retriever_with_mocks._fulltext_search("查询", None, 20, None)
        payload = retriever_with_mocks._http.post.call_args.kwargs.get("json", {})
        bool_clause = payload.get("query", {}).get("bool", {})
        assert "filter" not in bool_clause


class TestSearchPassesFilters:
    """search() 整体透传 filters 给三路（图谱路不接收）。

    P0-1: search() 现在会强制注入 doc_status=published 到 filters，
    所以传给三路的 filters 会比调用方传入的多一个 doc_status 键。
    """

    @pytest.mark.asyncio
    async def test_search_passes_filters_to_vector_and_fulltext(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """验证 search() 把 filters 传给 _vector_search 和 _fulltext_search。"""
        filters = {"series_id": "s1"}
        # P0-1: search() 会注入 doc_status=published
        expected_filters = {"series_id": "s1", "doc_status": "published"}
        with patch.object(
            retriever_with_mocks, "_vector_search", new=AsyncMock(return_value=[])
        ) as mock_vec, patch.object(
            retriever_with_mocks, "_fulltext_search", new=AsyncMock(return_value=[])
        ) as mock_ft, patch.object(
            retriever_with_mocks, "_cross_modal_search", new=AsyncMock(return_value=[])
        ) as mock_cm, patch.object(
            retriever_with_mocks, "_graph_search", new=AsyncMock(return_value=[])
        ) as mock_graph:
            await retriever_with_mocks.search("查询", ["kb1"], 20, filters)

        # 向量/全文/跨模态三路应收到含 doc_status 的 filters
        mock_vec.assert_called_once_with("查询", ["kb1"], 20, expected_filters)
        mock_ft.assert_called_once()
        ft_args = mock_ft.call_args.args
        assert ft_args[-1] == expected_filters  # 最后一个位置参数是 filters
        mock_cm.assert_called_once_with("查询", ["kb1"], 20, expected_filters)
        # 图谱路不接收 filters（只 3 个参数：entity_names, kb_ids, top_k）
        graph_args = mock_graph.call_args.args
        assert len(graph_args) == 3

    @pytest.mark.asyncio
    async def test_search_none_filters_backward_compatible(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """filters=None 时三路都应收到含 doc_status=published 的 dict（P0-1 注入）。"""
        # P0-1: filters=None 时 search() 也注入 doc_status=published
        expected_filters = {"doc_status": "published"}
        with patch.object(
            retriever_with_mocks, "_vector_search", new=AsyncMock(return_value=[])
        ) as mock_vec, patch.object(
            retriever_with_mocks, "_fulltext_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_cross_modal_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_graph_search", new=AsyncMock(return_value=[])
        ):
            await retriever_with_mocks.search("查询", ["kb1"], 20, None)

        mock_vec.assert_called_once_with("查询", ["kb1"], 20, expected_filters)
