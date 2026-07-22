"""安全 + 检索 5 项严重 bug 修复的回归测试。

Bug1: TokenCache 答案缓存 key 加入 tenant_id — 修复跨租户答案泄漏（安全）
Bug2: Milvus/OpenSearch upsert 写入真实 kb_id — 修复指定知识库检索永远为空
Bug3: HybridRetriever OpenSearch 索引名统一为配置常量 OPENSEARCH_INDEX，
      且故障后按重试窗口自动恢复（非粘性禁用）
Bug4: KnowledgeService.get_document / update_document 补密级校验 —
      修复越权读取 / 修改 secret 文档（安全）
Bug5: documents.py multipart 上传会话归属校验 + 文档列表租户过滤 —
      修复 IDOR 与跨租户数据可见（安全）
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.api.v1.documents import (
    _check_kb_write_access,
    _check_multipart_session,
    list_documents,
)
from app.config import get_settings
from app.rag.cache import TokenCache
from app.rag.chunker import Chunk
from app.rag.retriever import HybridRetriever
from app.rag.vector_store.base import VectorStoreBase
from app.rag.vector_store.milvus_store import MilvusVectorStore
from app.rag.vector_store.opensearch_store import OpenSearchVectorStore
from app.services.knowledge_service import KnowledgeService
from app.utils.pagination import PageResult


# ======================================================================
# 公共测试辅助
# ======================================================================


class _FakeEmbedder:
    """确定性假 embedder：同文本同向量，保证 L2 语义缓存命中路径可测。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.md5(t.encode("utf-8")).digest()
            out.append([b / 255.0 + 0.5 for b in digest[:8]])
        return out


class _MockResp:
    """模拟 httpx.Response。"""

    def __init__(self, status_code: int = 200, json_data: Any = None, text: str = "") -> None:
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


def _recording_http(post_response: _MockResp | None = None) -> tuple[MagicMock, list[dict], dict]:
    """创建记录 POST 调用参数的 mock http client。

    返回 (client, calls, state)。state["exc"] 置为非 None 时 post 抛异常，
    用于模拟 OpenSearch 故障 / 恢复场景。
    """
    calls: list[dict] = []
    state: dict[str, Any] = {"exc": None, "resp": post_response or _MockResp()}
    client = MagicMock()
    client.aclose = AsyncMock()

    async def _post(url: str, **kwargs: Any) -> _MockResp:
        calls.append({"url": url, **kwargs})
        if state["exc"] is not None:
            raise state["exc"]
        return state["resp"]

    client.post = _post
    client.get = AsyncMock(return_value=_MockResp())
    client.head = AsyncMock(return_value=_MockResp(status_code=200))
    client.put = AsyncMock(return_value=_MockResp())
    return client, calls, state


def _make_user(role: str = "viewer", clearance: str = "internal") -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), role=role, clearance_level=clearance, dept_id=None)


def _make_doc(classification: str = "internal") -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), kb_id=uuid4(), classification=classification, deleted_at=None)


def _make_chunk(doc_id: str = "doc-001") -> Chunk:
    return Chunk(
        id="chunk-0",
        doc_id=doc_id,
        content="这是用于验证 kb_id 写入的测试分块。",
        parent_id=None,
        start_pos=0,
        end_pos=20,
        token_count=8,
        title_path="标题",
        content_type="tutorial",
        chunk_strategy="structural",
    )


# ======================================================================
# Bug1: 答案缓存跨租户隔离
# ======================================================================


class TestBug1CacheTenantIsolation:
    """TokenCache 缓存 key 含 tenant_id — 跨租户答案不得泄漏。"""

    def test_hash_includes_tenant(self) -> None:
        """同 query 不同租户 / 无租户的哈希互不相同。"""
        h_a = TokenCache._hash("年假政策", "tenant-a")
        h_b = TokenCache._hash("年假政策", "tenant-b")
        h_none = TokenCache._hash("年假政策")
        assert len({h_a, h_b, h_none}) == 3

    @pytest.mark.asyncio
    async def test_l1_cross_tenant_isolated(self) -> None:
        """L1 Redis 精确缓存：租户 A 写入的答案对租户 B / 无租户调用不可见。"""
        store: dict[str, str] = {}
        redis = AsyncMock()

        async def _set(key: str, value: str, ex: int | None = None) -> None:
            store[key] = value

        async def _get(key: str) -> str | None:
            return store.get(key)

        redis.set = _set
        redis.get = _get
        cache = TokenCache(redis=redis, embedder=_FakeEmbedder())

        await cache.set("年假政策", "租户A的机密答案", tenant_id="tenant-a")

        # L1 key 必须按租户隔离
        key_a = f"cache:l1:{TokenCache._hash('年假政策', 'tenant-a')}"
        assert key_a in store
        assert f"cache:l1:{TokenCache._hash('年假政策', 'tenant-b')}" not in store

        # 同租户命中
        assert await cache.get("年假政策", tenant_id="tenant-a") == "租户A的机密答案"
        # 跨租户负向用例：不得命中
        assert await cache.get("年假政策", tenant_id="tenant-b") is None
        # 无租户（旧调用方）不得命中带租户的缓存
        assert await cache.get("年假政策") is None

    @pytest.mark.asyncio
    async def test_l2_semantic_cross_tenant_isolated(self) -> None:
        """L2 内存语义缓存：语义命中也必须限定同租户。"""
        cache = TokenCache(redis=None, embedder=_FakeEmbedder())
        cache._redis_available = False  # 跳过 L1，仅测 L2 语义路径

        await cache.set("报销流程是什么", "租户A的内部答案", tenant_id="tenant-a")

        # 同租户语义命中（同 query embedding 相同，相似度 1.0）
        assert await cache.get("报销流程是什么", tenant_id="tenant-a") == "租户A的内部答案"
        # 跨租户负向用例：即使语义完全相同也不得命中
        assert await cache.get("报销流程是什么", tenant_id="tenant-b") is None
        assert await cache.get("报销流程是什么") is None


# ======================================================================
# Bug2: 向量库 upsert 写入真实 kb_id
# ======================================================================


class TestBug2VectorStoreKbId:
    """upsert 的 kb_id 字段必须与检索端 kb_id 过滤对齐（真实知识库 ID）。"""

    def test_resolve_explicit_kb_id_wins(self) -> None:
        """显式入参 kb_id 优先。"""
        chunk = _make_chunk()
        object.__setattr__(chunk, "kb_id", "kb-from-chunk")  # Chunk 为 frozen dataclass
        assert VectorStoreBase._resolve_kb_id(chunk, "doc-1", "kb-explicit") == "kb-explicit"

    def test_resolve_chunk_carried_kb_id(self) -> None:
        """chunk 携带 kb_id 属性时次之。"""
        chunk = _make_chunk()
        object.__setattr__(chunk, "kb_id", "kb-from-chunk")
        assert VectorStoreBase._resolve_kb_id(chunk, "doc-1", None) == "kb-from-chunk"

    def test_resolve_fallback_doc_id_compatible(self) -> None:
        """均无 kb_id 时兜底 doc_id（兼容旧调用方行为）。"""
        chunk = _make_chunk(doc_id="doc-9")
        assert VectorStoreBase._resolve_kb_id(chunk, "doc-9", None) == "doc-9"

    @pytest.mark.asyncio
    async def test_milvus_upsert_writes_real_kb_id(self) -> None:
        """Milvus 写入的 kb_id 字段 = 真实知识库 ID，而非 doc_id。"""
        client, calls, _ = _recording_http()
        store = MilvusVectorStore(http_client=client)

        n = await store.upsert("doc-1", [_make_chunk()], [[0.1] * 8], kb_id="kb-real-1")

        assert n == 1
        records = calls[0]["json"]["data"]
        assert records[0]["kb_id"] == "kb-real-1"
        assert records[0]["doc_id"] == "doc-1"

    @pytest.mark.asyncio
    async def test_milvus_upsert_chunk_carried_kb_id(self) -> None:
        """Milvus：未传入参时写入 chunk 携带的 kb_id。"""
        client, calls, _ = _recording_http()
        store = MilvusVectorStore(http_client=client)
        chunk = _make_chunk()
        object.__setattr__(chunk, "kb_id", "kb-from-chunk")

        n = await store.upsert("doc-1", [chunk], [[0.1] * 8])

        assert n == 1
        assert calls[0]["json"]["data"][0]["kb_id"] == "kb-from-chunk"

    @pytest.mark.asyncio
    async def test_opensearch_upsert_writes_real_kb_id(self) -> None:
        """OpenSearch bulk 写入的 kb_id 字段 = 真实知识库 ID。"""
        client, calls, _ = _recording_http()
        store = OpenSearchVectorStore(http_client=client)
        store._index_ready = True  # 跳过 _ensure_index

        n = await store.upsert("doc-2", [_make_chunk()], [[0.1] * 8], kb_id="kb-real-2")

        assert n == 1
        body: str = calls[0]["content"]
        rows = [json.loads(line) for line in body.strip().split("\n")]
        doc_rows = [r for r in rows if "doc_id" in r]
        assert doc_rows[0]["kb_id"] == "kb-real-2"
        assert doc_rows[0]["doc_id"] == "doc-2"


# ======================================================================
# Bug3: retriever OpenSearch 索引名统一 + 故障可恢复
# ======================================================================


def _make_retriever(
    post_response: _MockResp | None = None,
) -> tuple[HybridRetriever, list[dict], dict]:
    client, calls, state = _recording_http(post_response)
    retriever = HybridRetriever(
        embedder=_FakeEmbedder(),
        http_client=client,
        vector_store=MagicMock(),
    )
    return retriever, calls, state


class TestBug3OpenSearchRetriever:
    """BM25 全文检索：索引名与写入方一致 + 失败后可自动恢复。"""

    @pytest.mark.asyncio
    async def test_fulltext_uses_configured_index(self) -> None:
        """查询 URL 必须使用统一配置 OPENSEARCH_INDEX（= 写入方 ekb_documents）。"""
        settings = get_settings()
        # 统一配置常量存在且与写入方索引一致
        assert settings.OPENSEARCH_INDEX == "ekb_documents"

        retriever, calls, _ = _make_retriever()
        await retriever._fulltext_search("报销流程", None, 5)

        assert len(calls) == 1
        assert calls[0]["url"] == f"{settings.OPENSEARCH_URL}/{settings.OPENSEARCH_INDEX}/_search"
        assert "document_chunks" not in calls[0]["url"]

    @pytest.mark.asyncio
    async def test_fulltext_kb_filter_uses_kb_id_field(self) -> None:
        """kb_ids 过滤必须作用于 kb_id 字段（与 Bug2 写入侧对齐）。"""
        retriever, calls, _ = _make_retriever()
        await retriever._fulltext_search("报销流程", ["kb-1", "kb-2"], 5)

        payload = calls[0]["json"]
        assert payload["query"]["bool"]["filter"] == [{"terms": {"kb_id": ["kb-1", "kb-2"]}}]

    @pytest.mark.asyncio
    async def test_failure_enters_retry_window_and_skips_calls(self) -> None:
        """故障后进入重试窗口：窗口内快速失败，不再发请求（避免拖垮检索）。"""
        retriever, calls, state = _make_retriever()
        state["exc"] = ConnectionError("opensearch down")

        assert await retriever._fulltext_search("q", None, 5) == []
        assert retriever._opensearch_available is False
        assert retriever._opensearch_retry_at > 0.0

        # 窗口内再次调用 — 直接返回空，不再发请求
        assert await retriever._fulltext_search("q", None, 5) == []
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_recovers_after_retry_window(self) -> None:
        """重试窗口过期后自动重探：成功即恢复可用（非粘性禁用）。"""
        hits = {
            "hits": {
                "hits": [
                    {
                        "_id": "chunk-1",
                        "_score": 1.5,
                        "_source": {
                            "doc_id": "doc-1",
                            "chunk_text": "报销流程内容",
                            "kb_id": "kb-1",
                            "title": "报销制度",
                        },
                    }
                ]
            }
        }
        retriever, calls, state = _make_retriever(_MockResp(json_data=hits))
        state["exc"] = ConnectionError("opensearch down")

        # 第一次：故障
        assert await retriever._fulltext_search("报销", None, 5) == []
        assert retriever._opensearch_available is False

        # 模拟重试窗口已过期 + 服务恢复
        retriever._opensearch_retry_at = 0.0
        state["exc"] = None

        results = await retriever._fulltext_search("报销", None, 5)
        assert retriever._opensearch_available is True
        assert retriever._opensearch_retry_at == 0.0
        assert len(calls) == 2  # 确实重新发起了探测请求
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc-1"


# ======================================================================
# Bug4: get_document / update_document 密级校验
# ======================================================================


def _make_service(
    user: SimpleNamespace,
    doc: SimpleNamespace | None,
    allowed: list[str],
) -> KnowledgeService:
    """构造绕过数据库的 KnowledgeService（permission 按给定密级白名单 mock）。"""
    svc = KnowledgeService(AsyncMock(), user, tenant_id=None)
    svc.doc_repo = AsyncMock()
    svc.doc_repo.get_by_id = AsyncMock(return_value=doc)
    perm = MagicMock()
    perm.check_function = AsyncMock(return_value=True)
    perm.allowed_classifications = MagicMock(return_value=allowed)
    svc.permission = perm
    return svc


class TestBug4ClassificationGuard:
    """密级超过用户 clearance_level 的文档禁止读取 / 修改。"""

    @pytest.mark.asyncio
    async def test_get_document_secret_denied(self) -> None:
        """负向：internal 用户读取 secret 文档 → PermissionError。"""
        user = _make_user(role="editor", clearance="internal")
        doc = _make_doc("secret")
        svc = _make_service(user, doc, allowed=["public", "internal"])

        with pytest.raises(PermissionError, match="密级不足"):
            await svc.get_document(doc.id)

    @pytest.mark.asyncio
    async def test_get_document_within_clearance_allowed(self) -> None:
        """正向：密级不超限的文档正常返回。"""
        user = _make_user(role="editor", clearance="internal")
        doc = _make_doc("internal")
        svc = _make_service(user, doc, allowed=["public", "internal"])

        result = await svc.get_document(doc.id)
        assert result is doc

    @pytest.mark.asyncio
    async def test_get_document_admin_full_clearance_allowed(self) -> None:
        """正向：admin（全量密级白名单）可读 secret 文档。"""
        user = _make_user(role="admin", clearance="secret")
        doc = _make_doc("secret")
        svc = _make_service(
            user, doc, allowed=["public", "internal", "confidential", "secret"]
        )

        result = await svc.get_document(doc.id)
        assert result is doc

    @pytest.mark.asyncio
    async def test_update_document_secret_denied(self) -> None:
        """负向：internal 用户修改 secret 文档 → PermissionError，且不触发写库。"""
        user = _make_user(role="editor", clearance="internal")
        doc = _make_doc("secret")
        svc = _make_service(user, doc, allowed=["public", "internal"])

        with pytest.raises(PermissionError, match="密级不足"):
            await svc.update_document(doc.id, content_text="篡改内容")
        svc.doc_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_document_within_clearance_allowed(self) -> None:
        """正向：密级不超限的文档正常更新。"""
        user = _make_user(role="editor", clearance="internal")
        doc = _make_doc("internal")
        svc = _make_service(user, doc, allowed=["public", "internal"])
        updated = _make_doc("internal")
        svc.doc_repo.update = AsyncMock(return_value=updated)

        result = await svc.update_document(doc.id, content_text="新内容")

        assert result is updated
        svc.doc_repo.update.assert_awaited_once_with(doc.id, content_text="新内容")


# ======================================================================
# Bug5: multipart 上传 IDOR + 文档列表租户过滤
# ======================================================================


class TestBug5MultipartSessionGuard:
    """_check_multipart_session 纯函数全分支测试。"""

    def test_missing_session_raises_404(self) -> None:
        """会话不存在 / 已过期 → 404。"""
        with pytest.raises(HTTPException) as exc_info:
            _check_multipart_session(None, _make_user())
        assert exc_info.value.status_code == 404

    def test_other_users_session_raises_403(self) -> None:
        """负向：非 admin 操作他人上传会话 → 403。"""
        session = {"user_id": str(uuid4()), "object_name": "kb-x/title"}
        with pytest.raises(HTTPException) as exc_info:
            _check_multipart_session(session, _make_user(role="viewer"))
        assert exc_info.value.status_code == 403

    def test_admin_may_operate_any_session(self) -> None:
        """admin 可操作任意会话（运维兜底）。"""
        session = {"user_id": str(uuid4()), "object_name": "kb-x/title"}
        _check_multipart_session(session, _make_user(role="admin"), "kb-x/title")

    def test_object_name_mismatch_raises_403(self) -> None:
        """负向：借用自有会话写入其他 object_name → 403。"""
        user = _make_user()
        session = {"user_id": str(user.id), "object_name": "kb-x/a"}
        with pytest.raises(HTTPException) as exc_info:
            _check_multipart_session(session, user, "kb-x/b")
        assert exc_info.value.status_code == 403

    def test_own_session_with_matching_object_name_passes(self) -> None:
        """正向：本人会话 + object_name 匹配 → 放行。"""
        user = _make_user()
        session = {"user_id": str(user.id), "object_name": "kb-x/a"}
        _check_multipart_session(session, user, "kb-x/a")

    def test_legacy_session_without_object_name_passes(self) -> None:
        """兼容：旧会话未记录 object_name 时不做绑定校验。"""
        user = _make_user()
        _check_multipart_session({"user_id": str(user.id)}, user, "kb-x/anything")


class TestBug5KbWriteAccess:
    """_check_kb_write_access — multipart init 的知识库写权限校验。"""

    @pytest.mark.asyncio
    async def test_no_write_permission_raises_403(self) -> None:
        """负向：对目标知识库无写权限 → 403。"""
        with patch("app.services.permission_service.PermissionService") as mock_cls:
            mock_cls.return_value.check_function = AsyncMock(return_value=False)
            with pytest.raises(HTTPException) as exc_info:
                await _check_kb_write_access(AsyncMock(), _make_user(), uuid4(), None)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_write_permission_passes(self) -> None:
        """正向：有写权限 → 放行。"""
        with patch("app.services.permission_service.PermissionService") as mock_cls:
            mock_cls.return_value.check_function = AsyncMock(return_value=True)
            await _check_kb_write_access(AsyncMock(), _make_user(), uuid4(), None)


# ----------------------------------------------------------------------
# Bug5: multipart 端点 HTTP 级负向用例
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def viewer_client():
    """带认证的 viewer（非 admin）客户端 — 用于 IDOR 负向用例。"""
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app
    from app.middleware import get_rate_limiter
    from app.models.user import User

    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    viewer = User(
        id=uuid4(),
        email="viewer@ekb.com",
        hashed_password="$2b$12$testhashplaceholderfor testing only",
        name="只读用户",
        role="viewer",
        clearance_level="internal",
        is_active=True,
    )

    async def override_user():
        return viewer

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        client._ekb_viewer = viewer  # type: ignore[attr-defined]
        yield client

    app.dependency_overrides.clear()


class TestBug5MultipartEndpointIDOR:
    """multipart 端点 HTTP 级越权负向用例（会话校验先于 MinIO 调用，无需 mock MinIO）。"""

    @pytest.mark.asyncio
    async def test_upload_part_other_session_403(self, viewer_client) -> None:
        """向他人上传会话注入分片 → 403。"""
        with patch(
            "app.api.v1.documents._load_multipart_session",
            new=AsyncMock(
                return_value={"user_id": str(uuid4()), "object_name": "kb-x/title"}
            ),
        ):
            resp = await viewer_client.put(
                f"/api/v1/documents/multipart/{uuid4()}/parts/1",
                params={"object_name": "kb-x/title"},
                files={"file": ("part.bin", b"data")},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_upload_part_missing_session_404(self, viewer_client) -> None:
        """会话不存在 → 404。"""
        with patch(
            "app.api.v1.documents._load_multipart_session",
            new=AsyncMock(return_value=None),
        ):
            resp = await viewer_client.put(
                f"/api/v1/documents/multipart/{uuid4()}/parts/1",
                params={"object_name": "kb-x/title"},
                files={"file": ("part.bin", b"data")},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_upload_part_object_name_mismatch_403(self, viewer_client) -> None:
        """借用自有会话但 object_name 不匹配 → 403。"""
        viewer_id = str(viewer_client._ekb_viewer.id)  # type: ignore[attr-defined]
        with patch(
            "app.api.v1.documents._load_multipart_session",
            new=AsyncMock(
                return_value={"user_id": viewer_id, "object_name": "kb-x/a"}
            ),
        ):
            resp = await viewer_client.put(
                f"/api/v1/documents/multipart/{uuid4()}/parts/1",
                params={"object_name": "kb-x/b"},
                files={"file": ("part.bin", b"data")},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_complete_other_session_403(self, viewer_client) -> None:
        """合并他人上传会话 → 403。"""
        payload = {
            "parts": [{"part_number": 1, "etag": "abc"}],
            "object_name": "kb-x/title",
            "kb_id": str(uuid4()),
            "title": "机密视频",
            "doc_type": "mp4",
        }
        with patch(
            "app.api.v1.documents._load_multipart_session",
            new=AsyncMock(
                return_value={"user_id": str(uuid4()), "object_name": "kb-x/title"}
            ),
        ):
            resp = await viewer_client.post(
                f"/api/v1/documents/multipart/{uuid4()}/complete",
                json=payload,
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_parts_other_session_403(self, viewer_client) -> None:
        """窥探他人上传会话的分片信息 → 403。"""
        with patch(
            "app.api.v1.documents._load_multipart_session",
            new=AsyncMock(
                return_value={"user_id": str(uuid4()), "object_name": "kb-x/title"}
            ),
        ):
            resp = await viewer_client.get(
                f"/api/v1/documents/multipart/{uuid4()}/parts",
                params={"object_name": "kb-x/title"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_abort_other_session_403(self, viewer_client) -> None:
        """恶意取消他人正在进行的上传 → 403。"""
        with patch(
            "app.api.v1.documents._load_multipart_session",
            new=AsyncMock(
                return_value={"user_id": str(uuid4()), "object_name": "kb-x/title"}
            ),
        ):
            resp = await viewer_client.delete(
                f"/api/v1/documents/multipart/{uuid4()}",
                params={"object_name": "kb-x/title"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_abort_missing_session_idempotent_200(self, viewer_client) -> None:
        """幂等：会话已不存在时 abort 仍返回成功（重复调用安全）。"""
        with (
            patch(
                "app.api.v1.documents._load_multipart_session",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.utils.minio_client.abort_multipart_upload",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await viewer_client.delete(
                f"/api/v1/documents/multipart/{uuid4()}",
                params={"object_name": "kb-x/title"},
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["aborted"] is True


# ----------------------------------------------------------------------
# Bug5: 文档列表租户过滤
# ----------------------------------------------------------------------


class TestBug5ListDocumentsTenantFilter:
    """GET /documents 必须按当前租户过滤，杜绝跨租户数据可见。"""

    @pytest.mark.asyncio
    async def test_tenant_filter_applied(self) -> None:
        """请求上下文带 tenant_id 时，查询必须包含 tenant_id 条件。"""
        tenant_id = uuid4()
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=tenant_id))
        admin = _make_user(role="admin")
        page = PageResult(items=[], total=0, page=1, size=20, pages=0)

        with patch("app.api.v1.documents.paginate", new=AsyncMock(return_value=page)) as mock_paginate:
            resp = await list_documents(
                request,
                kb_id=None,
                status_filter=None,
                keyword=None,
                page=1,
                size=20,
                db=AsyncMock(),
                user=admin,
            )

        stmt = mock_paginate.call_args.args[0]
        compiled = stmt.compile()
        assert "tenant_id" in str(compiled)
        assert tenant_id in compiled.params.values()
        assert resp.code == 0

    @pytest.mark.asyncio
    async def test_no_tenant_context_keeps_compatible(self) -> None:
        """无租户上下文（未启用多租户的部署）不加过滤，保持兼容。"""
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=None))
        admin = _make_user(role="admin")
        page = PageResult(items=[], total=0, page=1, size=20, pages=0)

        with patch("app.api.v1.documents.paginate", new=AsyncMock(return_value=page)) as mock_paginate:
            await list_documents(
                request,
                kb_id=None,
                status_filter=None,
                keyword=None,
                page=1,
                size=20,
                db=AsyncMock(),
                user=admin,
            )

        stmt = mock_paginate.call_args.args[0]
        # WHERE 条件不得包含 tenant_id 过滤参数（SELECT 列列表恒含该列名，需查参数）
        params = stmt.compile().params
        assert not any(str(key).startswith("tenant_id") for key in params)
