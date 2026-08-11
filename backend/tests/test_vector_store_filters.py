"""向量存储层级过滤测试 — P0 wiki 层级改造。

验证 OpenSearchVectorStore / MilvusVectorStore 的 search 方法正确将 filters
转为后端过滤条件写入 HTTP payload：
    - OpenSearch: payload.query.bool.filter 数组
    - Milvus: payload.filter expr 字符串

mock 策略：注入 AsyncMock http_client，捕获 post payload 验证。
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock celery（测试环境未安装）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
# httpx_retry 已在 app/utils/retry.py 中改为延迟导入，无需 sys.modules mock；
# 之前模块级 mock 会泄漏到 test_retry.py 导致 MagicMock 无法被 await。

from app.rag.vector_store.milvus_store import MilvusVectorStore
from app.rag.vector_store.opensearch_store import OpenSearchVectorStore
from app.utils.circuit_breaker import reset_all_circuit_breakers


@pytest.fixture(autouse=True)
def _reset_circuit_breakers() -> None:
    """每测试重置熔断器，避免熔断状态跨测试污染。"""
    reset_all_circuit_breakers()
    yield
    reset_all_circuit_breakers()


def _mock_response() -> MagicMock:
    """构造空结果的 mock HTTP 响应。"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"hits": {"hits": []}})
    return resp


class TestOpenSearchSearchFilters:
    """OpenSearchVectorStore.search filters → payload.query.bool.filter。"""

    @pytest.mark.asyncio
    async def test_filters_become_bool_filter(self) -> None:
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = OpenSearchVectorStore(http_client=http)
        store._available = True  # 跳过健康检查

        await store.search(
            [0.1] * 8, kb_ids=["kb1"], top_k=10, filters={"series_id": "s1", "depth": 0}
        )
        payload = http.post.call_args.kwargs.get("json", {})
        filter_clauses = payload["query"]["bool"]["filter"]
        assert {"term": {"series_id": "s1"}} in filter_clauses
        assert {"term": {"depth": 0}} in filter_clauses
        assert {"terms": {"kb_id": ["kb1"]}} in filter_clauses

    @pytest.mark.asyncio
    async def test_path_prefix_becomes_prefix_clause(self) -> None:
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = OpenSearchVectorStore(http_client=http)
        store._available = True

        await store.search(
            [0.1] * 8, filters={"path_prefix": "产品/合规/"}, top_k=10
        )
        payload = http.post.call_args.kwargs.get("json", {})
        filter_clauses = payload["query"]["bool"]["filter"]
        assert {"prefix": {"path": "产品/合规/"}} in filter_clauses

    @pytest.mark.asyncio
    async def test_no_filters_no_kb_ids_no_filter_array(self) -> None:
        """无 filters 且无 kb_ids 时，query 应为裸 knn 子句（无 bool.filter）。"""
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = OpenSearchVectorStore(http_client=http)
        store._available = True

        await store.search([0.1] * 8, top_k=10)
        payload = http.post.call_args.kwargs.get("json", {})
        # 无 filter 时 query 直接是 knn 子句（非 bool 包装）
        assert "knn" in payload["query"]

    @pytest.mark.asyncio
    async def test_kb_ids_only(self) -> None:
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = OpenSearchVectorStore(http_client=http)
        store._available = True

        await store.search([0.1] * 8, kb_ids=["kb1", "kb2"], top_k=10)
        payload = http.post.call_args.kwargs.get("json", {})
        filter_clauses = payload["query"]["bool"]["filter"]
        assert {"terms": {"kb_id": ["kb1", "kb2"]}} in filter_clauses


class TestMilvusSearchFilters:
    """MilvusVectorStore.search filters → payload.filter expr。"""

    @pytest.mark.asyncio
    async def test_filters_become_expr(self) -> None:
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = MilvusVectorStore(http_client=http)
        store._available = True

        await store.search(
            [0.1] * 8, kb_ids=["kb1"], top_k=10, filters={"series_id": "s1"}
        )
        payload = http.post.call_args.kwargs.get("json", {})
        expr = payload["filter"]
        assert "kb_id in [" in expr
        assert "series_id == 's1'" in expr
        assert " and " in expr

    @pytest.mark.asyncio
    async def test_path_prefix_uses_like(self) -> None:
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = MilvusVectorStore(http_client=http)
        store._available = True

        await store.search(
            [0.1] * 8, filters={"path_prefix": "产品/"}, top_k=10
        )
        payload = http.post.call_args.kwargs.get("json", {})
        assert payload["filter"] == "path like '产品/%'"

    @pytest.mark.asyncio
    async def test_depth_zero_in_expr(self) -> None:
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = MilvusVectorStore(http_client=http)
        store._available = True

        await store.search([0.1] * 8, filters={"depth": 0}, top_k=10)
        payload = http.post.call_args.kwargs.get("json", {})
        assert payload["filter"] == "depth == 0"

    @pytest.mark.asyncio
    async def test_no_filters_no_filter_field(self) -> None:
        """无 filters 且无 kb_ids 时，payload 不含 filter 字段。"""
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = MilvusVectorStore(http_client=http)
        store._available = True

        await store.search([0.1] * 8, top_k=10)
        payload = http.post.call_args.kwargs.get("json", {})
        assert "filter" not in payload

    @pytest.mark.asyncio
    async def test_kb_ids_only_expr(self) -> None:
        http = AsyncMock()
        http.post = AsyncMock(return_value=_mock_response())
        store = MilvusVectorStore(http_client=http)
        store._available = True

        await store.search([0.1] * 8, kb_ids=["kb1", "kb2"], top_k=10)
        payload = http.post.call_args.kwargs.get("json", {})
        expr = payload["filter"]
        assert "kb_id in [" in expr
        # milvus_store kb_id 用双引号（既有实现），filters 用单引号
        assert '"kb1"' in expr
        assert '"kb2"' in expr
        assert "series_id" not in expr
