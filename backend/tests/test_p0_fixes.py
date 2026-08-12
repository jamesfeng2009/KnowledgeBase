"""P0 修复测试 — 验证三处关键改动的正确性。

P0-1: 检索增加文档 status=published 过滤，杜绝半成品泄漏
P0-2: update_document 触发重建索引，消除混合状态
P0-3: conflict_resolver 保留冲突标记 + 补证触发，不让模型偷偷选边
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

from app.context.conflict_resolver import ConflictClaim, ConflictResolver
from app.rag.filter_builder import (
    build_milvus_expr,
    build_opensearch_combined_filter,
    build_opensearch_filter_clauses,
)
from app.rag.retriever import HybridRetriever


def _make_mock_tasks_module() -> MagicMock:
    """创建 mock tasks.document_tasks 模块（避免 celery_app 导入失败）。"""
    mock_mod = MagicMock()
    mock_process = MagicMock()
    mock_process.delay = MagicMock()
    mock_mod.process_document = mock_process
    return mock_mod


# ======================================================================
# P0-1: 检索增加文档 status=published 过滤
# ======================================================================


@pytest.fixture
def retriever_with_mocks() -> HybridRetriever:
    """构造注入 mock 依赖的 HybridRetriever。"""
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[[0.1] * 8])

    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])
    vector_store.fetch_by_ids = AsyncMock(return_value={})

    http_client = AsyncMock()
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


class TestP01RetrieverStatusFilter:
    """P0-1: search() 强制注入 doc_status=published 过滤。"""

    @pytest.mark.asyncio
    async def test_search_injects_doc_status_published_to_vector(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """search() 应在传给 _vector_search 的 filters 中注入 doc_status=published。"""
        with patch.object(
            retriever_with_mocks, "_vector_search", new=AsyncMock(return_value=[])
        ) as mock_vec, patch.object(
            retriever_with_mocks, "_fulltext_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_cross_modal_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_graph_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_expand_to_parents", new=AsyncMock(return_value=[])
        ):
            await retriever_with_mocks.search("查询", ["kb1"], 20)

        # filters 是第 4 个位置参数（query, kb_ids, top_k, filters）
        call_args = mock_vec.call_args
        filters = call_args.args[3] if len(call_args.args) >= 4 else call_args.kwargs.get("filters", {})
        assert filters.get("doc_status") == "published"

    @pytest.mark.asyncio
    async def test_search_injects_doc_status_published_to_fulltext(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """search() 应在传给 _fulltext_search 的 filters 中注入 doc_status=published。"""
        with patch.object(
            retriever_with_mocks, "_vector_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_fulltext_search", new=AsyncMock(return_value=[])
        ) as mock_ft, patch.object(
            retriever_with_mocks, "_cross_modal_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_graph_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_expand_to_parents", new=AsyncMock(return_value=[])
        ):
            await retriever_with_mocks.search("查询", ["kb1"], 20)

        call_args = mock_ft.call_args
        filters = call_args.args[3] if len(call_args.args) >= 4 else call_args.kwargs.get("filters", {})
        assert filters.get("doc_status") == "published"

    @pytest.mark.asyncio
    async def test_search_overrides_user_provided_doc_status(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """调用方传入 doc_status=draft 应被覆盖为 published（安全优先）。"""
        with patch.object(
            retriever_with_mocks, "_vector_search", new=AsyncMock(return_value=[])
        ) as mock_vec, patch.object(
            retriever_with_mocks, "_fulltext_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_cross_modal_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_graph_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_expand_to_parents", new=AsyncMock(return_value=[])
        ):
            await retriever_with_mocks.search(
                "查询", ["kb1"], 20, filters={"doc_status": "draft", "series_id": "s1"}
            )

        call_args = mock_vec.call_args
        filters = call_args.args[3] if len(call_args.args) >= 4 else call_args.kwargs.get("filters", {})
        # doc_status 必须被覆盖为 published
        assert filters["doc_status"] == "published"
        # 其他 filter 应保留
        assert filters["series_id"] == "s1"

    @pytest.mark.asyncio
    async def test_search_injects_doc_status_with_no_filters(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """filters=None 时也应注入 doc_status=published。"""
        with patch.object(
            retriever_with_mocks, "_vector_search", new=AsyncMock(return_value=[])
        ) as mock_vec, patch.object(
            retriever_with_mocks, "_fulltext_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_cross_modal_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_graph_search", new=AsyncMock(return_value=[])
        ), patch.object(
            retriever_with_mocks, "_expand_to_parents", new=AsyncMock(return_value=[])
        ):
            await retriever_with_mocks.search("查询", ["kb1"], 20, filters=None)

        call_args = mock_vec.call_args
        filters = call_args.args[3] if len(call_args.args) >= 4 else call_args.kwargs.get("filters", {})
        assert filters.get("doc_status") == "published"


class TestP01FilterBuilderDocStatus:
    """P0-1: filter_builder 正确构建 doc_status 过滤子句。"""

    def test_opensearch_filter_clauses_include_doc_status(self) -> None:
        """OpenSearch filter 子句应包含 doc_status term。"""
        clauses = build_opensearch_filter_clauses({"doc_status": "published"})
        assert {"term": {"doc_status": "published"}} in clauses

    def test_opensearch_combined_filter_includes_doc_status(self) -> None:
        """合并 kb_ids + filters 时 doc_status 应出现在 filter 数组中。"""
        clauses = build_opensearch_combined_filter(
            ["kb1"], {"doc_status": "published", "series_id": "s1"}
        )
        assert {"term": {"doc_status": "published"}} in clauses
        assert {"terms": {"kb_id": ["kb1"]}} in clauses

    def test_milvus_expr_includes_doc_status(self) -> None:
        """Milvus expr 应包含 doc_status == 'published'。"""
        expr = build_milvus_expr(["kb1"], {"doc_status": "published"})
        assert "doc_status == 'published'" in expr
        assert "kb_id in ['kb1']" in expr

    def test_milvus_expr_doc_status_only(self) -> None:
        """仅有 doc_status 过滤时 Milvus expr 正确。"""
        expr = build_milvus_expr(None, {"doc_status": "published"})
        assert expr == "doc_status == 'published'"


class TestP01FulltextSearchStatusFilter:
    """P0-1: 全文检索 payload 中包含 doc_status filter 子句。"""

    @pytest.mark.asyncio
    async def test_fulltext_payload_contains_doc_status_filter(
        self, retriever_with_mocks: HybridRetriever
    ) -> None:
        """_fulltext_search 收到 doc_status 过滤时，payload 中应有对应 term 子句。"""
        await retriever_with_mocks._fulltext_search(
            "查询", ["kb1"], 20, {"doc_status": "published"}
        )
        payload = retriever_with_mocks._http.post.call_args.kwargs.get("json", {})
        filter_clauses = payload.get("query", {}).get("bool", {}).get("filter", [])
        assert {"term": {"doc_status": "published"}} in filter_clauses


# ======================================================================
# P0-2: update_document 触发重建索引
# ======================================================================


class TestP02ReindexTrigger:
    """P0-2: update_document 触发重建索引。"""

    @pytest.mark.asyncio
    async def test_trigger_reindex_deletes_old_vectors(self) -> None:
        """_trigger_reindex 应先删除旧向量数据。"""
        from app.services.knowledge_service import KnowledgeService

        mock_db = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = MagicMock()
        mock_user.role = "admin"
        mock_user.dept_id = None
        service = KnowledgeService(mock_db, mock_user)

        mock_store = AsyncMock()
        mock_store.delete = AsyncMock()

        mock_tasks_mod = _make_mock_tasks_module()

        with patch.dict(sys.modules, {
            "tasks": mock_tasks_mod,
            "tasks.document_tasks": mock_tasks_mod,
        }), patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            await service._trigger_reindex("doc-123", "kb-456")

        mock_store.delete.assert_called_once_with("doc-123")

    @pytest.mark.asyncio
    async def test_trigger_reindex_calls_process_document_delay(self) -> None:
        """_trigger_reindex 应异步触发 process_document.delay()。"""
        from app.services.knowledge_service import KnowledgeService

        mock_db = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = MagicMock()
        mock_user.role = "admin"
        mock_user.dept_id = None
        service = KnowledgeService(mock_db, mock_user)

        mock_store = AsyncMock()
        mock_store.delete = AsyncMock()

        mock_tasks_mod = _make_mock_tasks_module()

        with patch.dict(sys.modules, {
            "tasks": mock_tasks_mod,
            "tasks.document_tasks": mock_tasks_mod,
        }), patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            await service._trigger_reindex("doc-123", "kb-456")

        mock_tasks_mod.process_document.delay.assert_called_once_with("doc-123")

    @pytest.mark.asyncio
    async def test_trigger_reindex_graceful_degradation_on_celery_failure(self) -> None:
        """Celery 不可用时 _trigger_reindex 不应抛异常。"""
        from app.services.knowledge_service import KnowledgeService

        mock_db = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = MagicMock()
        mock_user.role = "admin"
        mock_user.dept_id = None
        service = KnowledgeService(mock_db, mock_user)

        mock_store = AsyncMock()
        mock_store.delete = AsyncMock()

        mock_tasks_mod = _make_mock_tasks_module()
        mock_tasks_mod.process_document.delay = MagicMock(side_effect=Exception("Celery down"))

        with patch.dict(sys.modules, {
            "tasks": mock_tasks_mod,
            "tasks.document_tasks": mock_tasks_mod,
        }), patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            # 不应抛异常
            await service._trigger_reindex("doc-123", "kb-456")

    @pytest.mark.asyncio
    async def test_trigger_reindex_graceful_degradation_on_store_failure(self) -> None:
        """向量存储不可用时 _trigger_reindex 不应抛异常。"""
        from app.services.knowledge_service import KnowledgeService

        mock_db = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = MagicMock()
        mock_user.role = "admin"
        mock_user.dept_id = None
        service = KnowledgeService(mock_db, mock_user)

        mock_store = AsyncMock()
        mock_store.delete = AsyncMock(side_effect=Exception("OpenSearch down"))

        mock_tasks_mod = _make_mock_tasks_module()

        with patch.dict(sys.modules, {
            "tasks": mock_tasks_mod,
            "tasks.document_tasks": mock_tasks_mod,
        }), patch(
            "app.rag.vector_store.get_vector_store", return_value=mock_store
        ):
            # 不应抛异常
            await service._trigger_reindex("doc-123", "kb-456")
            # 即使删除失败，仍应尝试触发重建
            mock_tasks_mod.process_document.delay.assert_called_once_with("doc-123")


# ======================================================================
# P0-3: conflict_resolver 保留冲突标记 + 补证触发
# ======================================================================


class TestP03ConflictPreservation:
    """P0-3: 冲突裁决保留被否决声称 + 补证触发。"""

    def test_multi_source_conflict_preserves_losers(self) -> None:
        """多源冲突时 conflicting_claims 应保留所有被否决的声称。"""
        resolver = ConflictResolver()
        result = resolver.resolve([
            ConflictClaim("10000", "system_rule", "公司制度", key="报销上限"),
            ConflictClaim("5000", "tool_fact", "ERP", key="报销上限"),
            ConflictClaim("3000", "model_inference", "模型推测", key="报销上限"),
        ])
        assert result is not None
        assert result.resolved_value == "10000"
        assert len(result.conflicting_claims) == 2
        loser_values = {c.value for c in result.conflicting_claims}
        assert loser_values == {"5000", "3000"}

    def test_multi_source_conflict_triggers_clarification(self) -> None:
        """多源值冲突时 needs_clarification 应为 True。"""
        resolver = ConflictResolver()
        result = resolver.resolve([
            ConflictClaim("10天", "system_rule", "公司制度"),
            ConflictClaim("3天", "user_input", "用户"),
        ])
        assert result is not None
        assert result.needs_clarification is True

    def test_same_source_self_contradiction_no_clarification(self) -> None:
        """同源自我矛盾（last win）不触发补证。"""
        resolver = ConflictResolver()
        result = resolver.resolve([
            ConflictClaim("5000", "tool_fact", "ERP", "2026-01-01", key="报销上限"),
            ConflictClaim("8000", "tool_fact", "ERP", "2026-02-01", key="报销上限"),
        ])
        assert result is not None
        assert result.needs_clarification is False
        # 但仍保留被否决的声称供审计
        assert len(result.conflicting_claims) == 1
        assert result.conflicting_claims[0].value == "5000"

    def test_single_claim_no_conflicts(self) -> None:
        """单条声称无冲突 — conflicting_claims 为空，needs_clarification 为 False。"""
        resolver = ConflictResolver()
        result = resolver.resolve([
            ConflictClaim("唯一值", "system_rule", "制度"),
        ])
        assert result is not None
        assert result.conflicting_claims == []
        assert result.needs_clarification is False

    def test_multi_source_same_value_no_clarification(self) -> None:
        """多源声称值相同（非冲突）时不触发补证。"""
        resolver = ConflictResolver()
        result = resolver.resolve([
            ConflictClaim("10天", "system_rule", "公司制度"),
            ConflictClaim("10天", "user_input", "用户"),
        ], same_key_only=False)
        assert result is not None
        assert result.resolved_value == "10天"
        # 值相同 → 非真实冲突 → 不需要补证
        assert result.needs_clarification is False
        # 但仍保留被否决的声称（供审计追溯）
        assert len(result.conflicting_claims) == 1

    def test_authority_tie_preserves_losers(self) -> None:
        """权威并列 last win 时也保留被否决声称。"""
        resolver = ConflictResolver()
        result = resolver.resolve([
            ConflictClaim("旧值", "tool_fact", "ERP", "2026-01-01"),
            ConflictClaim("新值", "tool_fact", "ERP", "2026-03-01"),
        ], same_key_only=False)
        assert result is not None
        assert result.reason == "权威并列,last win"
        assert len(result.conflicting_claims) == 1
        assert result.conflicting_claims[0].value == "旧值"

    def test_conflicting_claims_contains_full_claim_objects(self) -> None:
        """conflicting_claims 应包含完整的 ConflictClaim 对象（含 source/authority）。"""
        resolver = ConflictResolver()
        result = resolver.resolve([
            ConflictClaim("10天", "system_rule", "公司制度"),
            ConflictClaim("3天", "user_input", "用户"),
        ])
        assert result is not None
        loser = result.conflicting_claims[0]
        assert isinstance(loser, ConflictClaim)
        assert loser.value == "3天"
        assert loser.authority == "user_input"
        assert loser.source == "用户"
