"""API 端点测试 — 通过 httpx AsyncClient 对 FastAPI 应用进行端到端测试。

验证点：
- test_health_check — 健康检查端点无需认证；
- test_login_required — 受保护端点未携带 token 返回 401；
- test_knowledge_list — 知识库列表（覆盖 KnowledgeService mock）；
- test_chat_stream — SSE 流式对话（覆盖 ChatService mock）；
- test_search — 混合搜索（覆盖 SearchService mock）；
- test_documents_crud — 文档详情查询（覆盖 KnowledgeService mock）。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.utils.pagination import PageResult


# ======================================================================
# Fixtures
# ======================================================================


@pytest_asyncio.fixture
async def raw_client():
    """无认证覆盖的客户端 — 用于测试认证强制。"""
    from app.main import app

    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client(mock_user):
    """带认证与 DB 覆盖的客户端 — 用于测试受保护端点。"""
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app

    async def override_user():
        return mock_user

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()


def _make_mock_kb() -> SimpleNamespace:
    """构造模拟知识库对象（兼容 KbResponse.model_validate）。"""
    return SimpleNamespace(
        id=uuid4(),
        name="测试知识库",
        description="用于测试",
        visibility="private",
        owner_id=uuid4(),
        dept_id=None,
        tags=["test"],
        created_at=datetime.now(),
        updated_at=datetime.now(),
        deleted_at=None,
        tenant_id=None,
    )


def _make_mock_doc() -> SimpleNamespace:
    """构造模拟文档对象（兼容 DocResponse.model_validate）。"""
    return SimpleNamespace(
        id=uuid4(),
        kb_id=uuid4(),
        title="测试文档",
        content_html="<p>测试内容</p>",
        content_json=None,
        content_text="测试内容",
        doc_type="md",
        status="published",
        owner_id=uuid4(),
        dept_id=None,
        classification="internal",
        view_count=0,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        deleted_at=None,
        tenant_id=None,
    )


# ======================================================================
# 测试
# ======================================================================


class TestHealthCheck:
    """健康检查测试。"""

    @pytest.mark.asyncio
    async def test_health_check(self, raw_client: httpx.AsyncClient) -> None:
        """GET /health 应返回 200 且 code=0。"""
        response = await raw_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["status"] == "ok"
        assert data["message"] == "success"


class TestLoginRequired:
    """认证强制测试。"""

    @pytest.mark.asyncio
    async def test_login_required(self, raw_client: httpx.AsyncClient) -> None:
        """未携带 Bearer token 访问受保护端点应返回 401。"""
        response = await raw_client.get("/api/v1/knowledge")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_search_requires_auth(self, raw_client: httpx.AsyncClient) -> None:
        """搜索端点同样需要认证。"""
        response = await raw_client.get("/api/v1/search", params={"q": "test"})

        assert response.status_code == 401


class TestKnowledgeList:
    """知识库列表测试。"""

    @pytest.mark.asyncio
    async def test_knowledge_list(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/knowledge 应返回知识库分页列表。"""
        mock_kb = _make_mock_kb()
        mock_result = PageResult(
            items=[mock_kb], total=1, page=1, size=20, pages=1
        )

        with patch("app.api.v1.knowledge.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.list_kbs = AsyncMock(return_value=mock_result)

            response = await auth_client.get("/api/v1/knowledge")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["total"] == 1
        assert data["data"]["items"][0]["name"] == "测试知识库"

    @pytest.mark.asyncio
    async def test_knowledge_list_pagination(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """分页参数应正确传递给 service。"""
        mock_result = PageResult(
            items=[], total=0, page=2, size=10, pages=0
        )

        with patch("app.api.v1.knowledge.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.list_kbs = AsyncMock(return_value=mock_result)

            response = await auth_client.get(
                "/api/v1/knowledge", params={"page": 2, "size": 10}
            )

        assert response.status_code == 200
        assert response.json()["data"]["page"] == 2
        mock_service.list_kbs.assert_called_once_with(page=2, size=10)


class TestChatStream:
    """SSE 流式对话测试。"""

    @pytest.mark.asyncio
    async def test_chat_stream(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """POST /api/v1/chat 应返回 SSE 流。"""

        class _FakeChatService:
            async def chat(self, **kwargs):
                yield "data: 你好\n\n"
                yield "event: done\ndata: {}\n\n"

        with patch("app.api.v1.chat.ChatService", return_value=_FakeChatService()):
            response = await auth_client.post(
                "/api/v1/chat",
                json={"query": "测试问题", "agent_type": "qa"},
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        body = response.text
        assert "你好" in body
        assert "done" in body


class TestSearch:
    """搜索端点测试。"""

    @pytest.mark.asyncio
    async def test_search(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/search?q=... 应返回搜索结果。"""
        doc_id = uuid4()
        mock_result = {
            "results": [
                {
                    "doc_id": str(doc_id),
                    "title": "报销流程指南",
                    "snippet": "员工报销需先提交...",
                    "score": 0.95,
                }
            ],
            "total": 1,
            "query": "报销",
        }

        with patch("app.api.v1.search.SearchService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.search = AsyncMock(return_value=mock_result)

            response = await auth_client.get(
                "/api/v1/search", params={"q": "报销", "search_type": "hybrid"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["total"] == 1
        assert data["data"]["query"] == "报销"
        assert data["data"]["results"][0]["title"] == "报销流程指南"

    @pytest.mark.asyncio
    async def test_search_empty_results(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """无匹配结果时返回空列表。"""
        mock_result = {"results": [], "total": 0, "query": "不存在的词"}

        with patch("app.api.v1.search.SearchService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.search = AsyncMock(return_value=mock_result)

            response = await auth_client.get(
                "/api/v1/search", params={"q": "不存在的词"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["total"] == 0
        assert data["data"]["results"] == []


class TestDocumentsCrud:
    """文档 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_documents_crud(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/documents/{doc_id} 应返回文档详情。"""
        mock_doc = _make_mock_doc()

        # /documents/{doc_id} 路由在 knowledge 路由中优先注册
        with patch("app.api.v1.knowledge.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(return_value=mock_doc)

            response = await auth_client.get(f"/api/v1/documents/{mock_doc.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["title"] == "测试文档"
        assert data["data"]["doc_type"] == "md"

    @pytest.mark.asyncio
    async def test_document_not_found(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """文档不存在时返回 404。"""
        doc_id = uuid4()

        with patch("app.api.v1.knowledge.KnowledgeService") as mock_cls:
            mock_service = mock_cls.return_value
            mock_service.get_document = AsyncMock(
                side_effect=ValueError(f"文档不存在: {doc_id}")
            )

            response = await auth_client.get(f"/api/v1/documents/{doc_id}")

        assert response.status_code == 404
        assert response.json()["code"] == 404
