"""检索不变量契约测试 — Phase 0 堵漏（GAP-2）第 3 层锁定。

三层防御（详见 app/rag/retrieval_invariants.py）：
    第 1 层 Pushdown — 向量 / 全文 / 跨模态统一注入 doc_status=published
    第 1 层 Cypher  — 图谱路在 graph_service.py 源头过滤（含建图写入属性）
    第 2 层 Final Gate — filter_retrieval_candidates 三项复检（DB 权威源）

本文件以 parametrize 锁定 ALL_CHANNELS 注册表中全部通道：
draft / 越权 / 跨租户候选必须不出现。新增检索通道必须注册进
ALL_CHANNELS 并通过契约测试，否则 CI 红灯。

mock 策略：全部外部依赖（向量库 / OpenSearch / Neo4j / PostgreSQL）
以 AsyncMock 注入，不依赖真实服务。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Mock celery（测试环境未安装）— 与 test_retriever_filters.py 同款
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

from app.rag.retrieval_invariants import ALL_CHANNELS, RetrievalInvariants
from app.rag.retriever import HybridRetriever
from app.services.graph_service import GraphService
from app.services.permission_service import PermissionService

# ======================================================================
# 场景数据 — 同一 KB 下 1 篇 published + 1 篇 draft，
# 再造 1 篇越权密级 published + 1 篇跨 KB published
# ======================================================================

KB_ID = str(uuid4())
OTHER_KB_ID = str(uuid4())
PUBLISHED_ID = str(uuid4())
DRAFT_ID = str(uuid4())
SECRET_ID = str(uuid4())
OTHER_KB_DOC_ID = str(uuid4())

# DB 权威状态表：{doc_id: (classification, status)}
DOC_META_ROWS = [
    (PUBLISHED_ID, "internal", "published"),
    (DRAFT_ID, "internal", "draft"),
    (SECRET_ID, "secret", "published"),
    (OTHER_KB_DOC_ID, "internal", "published"),
]


def _candidate(doc_id: str, kb_id: str, source: str = "vector") -> dict[str, Any]:
    """构造 HybridRetriever 统一格式的候选 dict。"""
    return {
        "doc_id": doc_id,
        "chunk_id": f"{source}_{doc_id}",
        "content": f"content-{doc_id}",
        "score": 0.9,
        "source": source,
        "kb_id": kb_id,
        "title": f"title-{doc_id}",
    }


def _polluted_results() -> list[dict[str, Any]]:
    """模拟被污染的召回结果 — 四类候选混入（draft / 越密级 / 跨 KB / 正常）。"""
    return [
        _candidate(PUBLISHED_ID, KB_ID),
        _candidate(DRAFT_ID, KB_ID),
        _candidate(SECRET_ID, KB_ID),
        _candidate(OTHER_KB_DOC_ID, OTHER_KB_ID),
    ]


def _make_perm_svc(
    role: str = "editor",
    clearance: str = "internal",
    accessible_kbs: list[str] | None = None,
) -> PermissionService:
    """构造 DB mock 的 PermissionService。

    execute 调用序列（filter_retrieval_candidates 内部）：
        1. _load_doc_meta → [(doc_id, classification, status), ...]
        2. get_accessible_kb_ids → [(kb_id,), ...]（admin 不调用）
    """
    if accessible_kbs is None:
        accessible_kbs = [KB_ID]

    user = SimpleNamespace(
        id=uuid4(), role=role, clearance_level=clearance
    )
    db = AsyncMock()

    meta_result = MagicMock()
    meta_result.all.return_value = DOC_META_ROWS
    kb_result = MagicMock()
    kb_result.all.return_value = [(kb,) for kb in accessible_kbs]
    db.execute = AsyncMock(side_effect=[meta_result, kb_result])
    return PermissionService(db=db, user=user)


# ======================================================================
# 第 1 层 · Pushdown 单元契约
# ======================================================================


class TestPushdown:
    """RetrievalInvariants.pushdown — I1 下推子句的唯一权威定义。"""

    def test_injects_published(self) -> None:
        filters = RetrievalInvariants.pushdown("vector", ["kb1"], None)
        assert filters["doc_status"] == "published"

    def test_overrides_caller_draft(self) -> None:
        """调用方传 doc_status=draft 必须被覆盖 — 安全优先于灵活性。"""
        filters = RetrievalInvariants.pushdown(
            "fulltext", ["kb1"], {"doc_status": "draft"}
        )
        assert filters["doc_status"] == "published"

    def test_does_not_mutate_base(self) -> None:
        base = {"series_id": "s1"}
        RetrievalInvariants.pushdown("cross_modal", None, base)
        assert "doc_status" not in base  # 原 dict 不被污染

    def test_preserves_other_keys(self) -> None:
        filters = RetrievalInvariants.pushdown(
            "vector", None, {"series_id": "s1", "depth": 2}
        )
        assert filters["series_id"] == "s1"
        assert filters["depth"] == 2


# ======================================================================
# 第 1 层 · Pushdown 通道契约 — parametrize 全部下推通道
# ======================================================================


def _make_retriever() -> HybridRetriever:
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[[0.1] * 8])
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])
    http_client = AsyncMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"hits": {"hits": []}})
    http_client.post = AsyncMock(return_value=resp)
    return HybridRetriever(
        embedder=embedder, http_client=http_client, vector_store=vector_store
    )


async def _check_channel_pushdown(channel: str) -> None:
    """验证通道在 search() 调用链上收到 doc_status=published 下推子句。"""
    retriever = _make_retriever()
    with patch.object(
        retriever, "_vector_search", new=AsyncMock(return_value=[])
    ) as vec, patch.object(
        retriever, "_fulltext_search", new=AsyncMock(return_value=[])
    ) as ft, patch.object(
        retriever, "_cross_modal_search", new=AsyncMock(return_value=[])
    ) as cm, patch.object(
        retriever, "_graph_search", new=AsyncMock(return_value=[])
    ):
        await retriever.search("查询", [KB_ID], 20, {"series_id": "s1"})

    expected = {"series_id": "s1", "doc_status": "published"}
    if channel == "vector":
        # filters 为第 4 个位置参数（query, kb_ids, top_k, filters）
        assert vec.call_args.args[-1] == expected
    elif channel == "fulltext":
        assert ft.call_args.args[-1] == expected
    elif channel == "cross_modal":
        assert cm.call_args.args[-1] == expected
    else:
        raise ValueError(f"未实现的下推通道检查: {channel}")


class TestPushdownChannelContract:
    """全部下推通道必须经由 RetrievalInvariants.pushdown 接收 I1 子句。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["vector", "fulltext", "cross_modal"])
    async def test_channel_receives_published_filter(self, channel: str) -> None:
        await _check_channel_pushdown(channel)

    @pytest.mark.asyncio
    async def test_fulltext_backend_clause_contains_published(self) -> None:
        """BM25 路：doc_status 下推必须落到 OpenSearch bool.filter 子句。"""
        retriever = _make_retriever()
        await retriever._fulltext_search(
            "查询", [KB_ID], 20, RetrievalInvariants.pushdown(
                "fulltext", [KB_ID], None
            )
        )
        payload = retriever._http.post.call_args.kwargs.get("json", {})
        clauses = payload.get("query", {}).get("bool", {}).get("filter", [])
        assert {"term": {"doc_status": "published"}} in clauses

    @pytest.mark.asyncio
    async def test_vector_backend_receives_filters(self) -> None:
        """向量路：filters 必须透传给 VectorStoreBase.search（后端转 filter 子句）。"""
        retriever = _make_retriever()
        await retriever._vector_search(
            "查询", [KB_ID], 20, RetrievalInvariants.pushdown(
                "vector", [KB_ID], None
            )
        )
        kwargs = retriever._vector_store.search.call_args.kwargs
        assert kwargs.get("filters", {}).get("doc_status") == "published"


# ======================================================================
# 第 1 层 · 图谱路 Cypher 契约 — 源头过滤
# ======================================================================


class _FakeResult:
    def __init__(self, rows: list | None = None) -> None:
        self._rows = rows or []

    async def data(self) -> list:
        return self._rows

    async def single(self) -> Any:
        return None


class _FakeSession:
    queries: list[str] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def run(self, query: str, **params: Any) -> _FakeResult:
        _FakeSession.queries.append(query)
        return _FakeResult([])


class _FakeDriver:
    def session(self, database: str | None = None) -> _FakeSession:
        return _FakeSession()


def _make_graph_service() -> GraphService:
    service = GraphService()
    service._driver = _FakeDriver()
    service._ensure_connected = AsyncMock(return_value=True)  # type: ignore[method-assign]
    return service


class TestGraphCypherContract:
    """图谱路第 1 层 — Cypher 必须在源头过滤 doc_status='published'。"""

    @pytest.mark.asyncio
    async def test_chunk_level_query_filters_published(self) -> None:
        _FakeSession.queries = []
        service = _make_graph_service()
        await service.find_related_documents_by_entity(["实体A"])
        assert _FakeSession.queries, "应执行至少一条 Cypher"
        chunk_queries = [q for q in _FakeSession.queries if "HAS_CHUNK" in q]
        assert chunk_queries, "应包含 chunk 级遍历查询"
        assert all("d.doc_status = 'published'" in q for q in chunk_queries)

    @pytest.mark.asyncio
    async def test_document_fallback_query_filters_published(self) -> None:
        _FakeSession.queries = []
        service = _make_graph_service()
        await service.find_related_documents_by_entity(["实体A"])
        # 回退查询特征：直接 MENTIONS 到 Document（含 NOT HAS_CHUNK 排除子句）
        fallback_queries = [
            q for q in _FakeSession.queries
            if "MENTIONS]-(d:Document)" in q
        ]
        assert fallback_queries, "应包含 Document 级回退查询"
        assert all("d.doc_status = 'published'" in q for q in fallback_queries)

    @pytest.mark.asyncio
    async def test_batch_import_document_writes_doc_status(self) -> None:
        """建图契约 — Document 节点必须携带 doc_status 属性（Cypher 过滤依赖）。"""
        service = _make_graph_service()
        captured: dict[str, Any] = {}

        async def fake_import(nodes: list, relationships: list, **kw: Any) -> dict:
            captured["nodes"] = nodes
            return {"nodes_created": len(nodes), "relationships_created": 0}

        service.batch_import_graph = fake_import  # type: ignore[method-assign]
        await service.batch_import_document(
            doc_id=str(uuid4()), title="t", content="c", kb_id=KB_ID,
            doc_status="published",
        )
        doc_nodes = [n for n in captured["nodes"] if n.get("label") == "Document"]
        assert doc_nodes and doc_nodes[0].get("doc_status") == "published"

    @pytest.mark.asyncio
    async def test_batch_import_document_default_published(self) -> None:
        """默认参数 published — 无状态调用方（历史兼容）建图即已发布语义。"""
        service = _make_graph_service()
        captured: dict[str, Any] = {}

        async def fake_import(nodes: list, relationships: list, **kw: Any) -> dict:
            captured["nodes"] = nodes
            return {"nodes_created": len(nodes), "relationships_created": 0}

        service.batch_import_graph = fake_import  # type: ignore[method-assign]
        await service.batch_import_document(
            doc_id=str(uuid4()), title="t", content="c", kb_id=KB_ID
        )
        doc_nodes = [n for n in captured["nodes"] if n.get("label") == "Document"]
        assert doc_nodes and doc_nodes[0].get("doc_status") == "published"

    @pytest.mark.asyncio
    async def test_sync_doc_status_emits_set_clause(self) -> None:
        """存量回填 — UNWIND 批量 SET doc_status（幂等迁移入口）。"""
        _FakeSession.queries = []
        service = _make_graph_service()
        updated = await service.sync_doc_status(
            {str(uuid4()): "published", str(uuid4()): "draft"}
        )
        assert _FakeSession.queries
        assert any("SET d.doc_status" in q for q in _FakeSession.queries)
        assert updated == 0  # FakeResult.single 返回 None → 不计数，不报错


# ======================================================================
# 第 2 层 · Final Gate 单元契约
# ======================================================================


class TestFinalGate:
    """RetrievalInvariants.final_gate — 注入前最后一道门。"""

    @pytest.mark.asyncio
    async def test_empty_results_short_circuit(self) -> None:
        result = await RetrievalInvariants.final_gate([], perm_svc=None)
        assert result == []

    @pytest.mark.asyncio
    async def test_delegates_to_perm_svc(self) -> None:
        results = _polluted_results()
        safe = [_candidate(PUBLISHED_ID, KB_ID)]
        perm_svc = SimpleNamespace(
            filter_retrieval_candidates=AsyncMock(return_value=safe)
        )
        out = await RetrievalInvariants.final_gate(results, perm_svc=perm_svc)
        perm_svc.filter_retrieval_candidates.assert_awaited_once_with(results)
        assert out == safe

    @pytest.mark.asyncio
    async def test_no_perm_svc_passthrough(self) -> None:
        """无权限服务（如内部 pipeline 评测）— 保持既有行为原样返回。"""
        results = _polluted_results()
        out = await RetrievalInvariants.final_gate(results, perm_svc=None)
        assert out == results

    @pytest.mark.asyncio
    async def test_error_fail_closed(self) -> None:
        """复检异常 → 全部剔除（宁可少召回不可放行未验证内容）。"""
        perm_svc = SimpleNamespace(
            filter_retrieval_candidates=AsyncMock(side_effect=RuntimeError("db down"))
        )
        out = await RetrievalInvariants.final_gate(
            _polluted_results(), perm_svc=perm_svc
        )
        assert out == []


# ======================================================================
# 第 2 层 · Final Gate 行为契约 — 三项复检（DB 权威源）
# ======================================================================


class TestFinalGateBehavior:
    """filter_retrieval_candidates — I1 状态 + I3 密级 + I4 归属复检。"""

    @pytest.mark.asyncio
    async def test_i1_draft_blocked(self) -> None:
        svc = _make_perm_svc()
        out = await svc.filter_retrieval_candidates(_polluted_results())
        assert all(r["doc_id"] != DRAFT_ID for r in out)

    @pytest.mark.asyncio
    async def test_i1_draft_blocked_for_admin(self) -> None:
        """admin 放行密级 / 归属，但 I1 状态复检对 admin 同样生效。"""
        svc = _make_perm_svc(role="admin", clearance="secret")
        out = await svc.filter_retrieval_candidates(_polluted_results())
        assert all(r["doc_id"] != DRAFT_ID for r in out)

    @pytest.mark.asyncio
    async def test_i3_clearance_blocked(self) -> None:
        """secret 文档对 internal 用户 — 越密级剔除。"""
        svc = _make_perm_svc(clearance="internal")
        out = await svc.filter_retrieval_candidates(_polluted_results())
        assert all(r["doc_id"] != SECRET_ID for r in out)

    @pytest.mark.asyncio
    async def test_i4_kb_scope_blocked(self) -> None:
        """跨 KB 文档（不在可访问集合）剔除。"""
        svc = _make_perm_svc()
        out = await svc.filter_retrieval_candidates(_polluted_results())
        assert all(r["doc_id"] != OTHER_KB_DOC_ID for r in out)

    @pytest.mark.asyncio
    async def test_fail_closed_missing_in_db(self) -> None:
        """DB 查不到的 doc_id（已删除 / 非法）— fail-closed 剔除。"""
        svc = _make_perm_svc()
        ghost = _candidate(str(uuid4()), KB_ID)
        out = await svc.filter_retrieval_candidates([ghost])
        assert out == []

    @pytest.mark.asyncio
    async def test_fail_closed_kb_missing(self) -> None:
        """kb_id 缺失的候选 — 无法确认归属不放行。"""
        svc = _make_perm_svc()
        orphan = _candidate(PUBLISHED_ID, "")
        orphan["kb_id"] = None
        out = await svc.filter_retrieval_candidates([orphan])
        assert out == []

    @pytest.mark.asyncio
    async def test_published_internal_kept(self) -> None:
        """合法候选（published + internal + 授权 KB）正常保留。"""
        svc = _make_perm_svc()
        out = await svc.filter_retrieval_candidates(_polluted_results())
        assert [r["doc_id"] for r in out] == [PUBLISHED_ID]


# ======================================================================
# 第 3 层 · 全通道契约 — parametrize ALL_CHANNELS（注册制）
# ======================================================================


async def _run_channel_contract(channel: str) -> None:
    """每个注册通道的「draft 不可见」端到端契约。

    - 下推通道（vector/fulltext/cross_modal）：验证 I1 子句在调用链上
      下推到该通道（索引层过滤由后端 filter 子句执行）。
    - graph 通道：验证 Cypher 源头过滤 + kb 内存过滤保留。
    - constraint 桩通道：验证不变量逻辑与具体后端解耦 — pushdown +
      final_gate 组合对污染输入的裁决。
    """
    if channel in ("vector", "fulltext", "cross_modal"):
        await _check_channel_pushdown(channel)
        return

    if channel == "graph":
        _FakeSession.queries = []
        service = _make_graph_service()
        await service.find_related_documents_by_entity(["实体A"])
        assert all(
            "d.doc_status = 'published'" in q for q in _FakeSession.queries
        ), "图谱路全部 Cypher 必须含 doc_status 过滤"
        # kb_id 内存过滤保留（retriever._graph_search L481-487）
        retriever = _make_retriever()
        related = [
            {"doc_id": PUBLISHED_ID, "kb_id": KB_ID, "title": "t"},
            {"doc_id": OTHER_KB_DOC_ID, "kb_id": OTHER_KB_ID, "title": "t2"},
        ]
        with patch(
            "app.services.graph_service.GraphService"
        ) as MockGraph:
            MockGraph.return_value.find_related_documents_by_entity = (
                AsyncMock(return_value=related)
            )
            from app.config import get_settings as _gs

            with patch.object(_gs(), "GRAPH_SEARCH_ENABLED", True):
                results = await retriever._graph_search(
                    ["实体A"], [KB_ID], 10
                )
        assert all(r["kb_id"] == KB_ID for r in results)
        return

    if channel == "constraint":
        # 桩通道：不变量逻辑与后端解耦 — 污染输入经 pushdown + final_gate
        # 后 draft / 越权 / 跨 KB 全部被裁决剔除
        svc = _make_perm_svc()
        safe = await RetrievalInvariants.final_gate(
            _polluted_results(), kb_ids=[KB_ID], perm_svc=svc
        )
        assert [r["doc_id"] for r in safe] == [PUBLISHED_ID]
        return

    raise ValueError(f"通道 {channel} 已注册但缺少契约检查 — 新增通道必须实现检查")


class TestChannelContract:
    """全部注册通道锁定 — 新增通道不实现检查即 CI 红灯。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ALL_CHANNELS)
    async def test_draft_document_never_recalled(self, channel: str) -> None:
        """I1 契约：任何通道的检索输出不得包含 draft 文档。"""
        await _run_channel_contract(channel)

    def test_all_channels_registered(self) -> None:
        """通道注册表完整性 — 与实现检查的通道集一致。"""
        assert set(ALL_CHANNELS) == {
            "vector", "fulltext", "cross_modal", "graph", "constraint"
        }


# ======================================================================
# 端到端兜底 — 第 1 层全部失效时，Final Gate 仍拦截
# ======================================================================


class TestEndToEndBackstop:
    """模拟索引被污染 / 下推被绕过的最坏情况 — Final Gate 兜底。"""

    @pytest.mark.asyncio
    async def test_polluted_index_blocked_by_final_gate(self) -> None:
        """四路全部返回 draft（模拟向量化残留 / 图谱漏过滤）— DB 复检全拦。"""
        polluted = [
            _candidate(DRAFT_ID, KB_ID, source)
            for source in ("vector", "fulltext", "cross_modal", "graph")
        ] + [_candidate(PUBLISHED_ID, KB_ID)]
        svc = _make_perm_svc()
        safe = await RetrievalInvariants.final_gate(polluted, perm_svc=svc)
        assert [r["doc_id"] for r in safe] == [PUBLISHED_ID]
