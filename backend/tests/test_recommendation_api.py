"""知识推荐 API 测试 — 通过 httpx AsyncClient 对推荐端点做端到端测试。

验证点：
- 认证强制：未携带 token 返回 401；
- 模块门控：套餐未启用推荐模块返回 403；
- GET /recommendations/user — 个性化推荐（含权限过滤调用）；
- GET /recommendations/document/{doc_id} — 相关阅读（含非法 ID 校验）；
- POST /recommendations/feedback — 行为上报（含非法 action/doc_id 校验）；
- POST /recommendations/rebuild — 管理员权限校验。

使用 mock 隔离外部依赖（TenantService / RecommendationService / PermissionService），
不依赖外部服务。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio


# ======================================================================
# 工具与 Fixtures
# ======================================================================


def _make_user(role: str = "editor") -> SimpleNamespace:
    """构造模拟用户对象。"""
    return SimpleNamespace(
        id=uuid4(),
        role=role,
        is_active=True,
        email="u@test.com",
        name="测试用户",
    )


def _make_recommend_item(doc_id: str | None = None) -> dict:
    """构造推荐结果条目。"""
    return {
        "doc_id": str(doc_id or uuid4()),
        "title": "推荐文档",
        "reason": "hot",
        "score": 0.5,
    }


@pytest_asyncio.fixture
async def raw_client():
    """无认证覆盖的客户端 — 用于测试认证强制。"""
    from app.main import app
    from app.middleware import get_rate_limiter

    # 清理限流器 buckets，防止跨测试触发 429
    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client():
    """带认证与 DB 覆盖的客户端 — 用于测试受保护端点。"""
    from app.database import get_db_session
    from app.deps import get_current_user
    from app.main import app
    from app.middleware import get_rate_limiter

    # 清理限流器 buckets，防止跨测试触发 429
    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    mock_user = _make_user()

    async def override_user():
        return mock_user

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    # 推荐模块门控默认放行：mock TenantService.is_module_enabled
    module_enabled_patch = patch(
        "app.services.tenant_service.TenantService.is_module_enabled",
        new=AsyncMock(return_value=True),
    )
    module_enabled_patch.start()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    module_enabled_patch.stop()
    app.dependency_overrides.clear()


# ======================================================================
# 认证与门控
# ======================================================================


class TestRecommendationAuth:
    """认证强制与模块门控测试。"""

    @pytest.mark.asyncio
    async def test_user_recommendations_requires_auth(
        self, raw_client: httpx.AsyncClient
    ) -> None:
        """未携带 token 访问个性化推荐应返回 401。"""
        response = await raw_client.get("/api/v1/recommendations/user")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_feedback_requires_auth(self, raw_client: httpx.AsyncClient) -> None:
        """未携带 token 上报行为应返回 401。"""
        response = await raw_client.post(
            "/api/v1/recommendations/feedback",
            json={"doc_id": str(uuid4()), "action_type": "view"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_module_gating_forbidden(self) -> None:
        """套餐未启用推荐模块时应返回 403。"""
        from app.database import get_db_session
        from app.deps import get_current_user
        from app.main import app
        from app.middleware import get_rate_limiter

        limiter = get_rate_limiter()
        if limiter is not None:
            limiter.clear()

        mock_user = _make_user()

        async def override_user():
            return mock_user

        async def override_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[get_db_session] = override_db

        # 模块未启用 → 403
        with patch(
            "app.services.tenant_service.TenantService.is_module_enabled",
            new=AsyncMock(return_value=False),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/v1/recommendations/user")

        app.dependency_overrides.clear()
        assert response.status_code == 403


# ======================================================================
# 个性化推荐
# ======================================================================


class TestUserRecommendations:
    """GET /api/v1/recommendations/user 测试。"""

    @pytest.mark.asyncio
    async def test_returns_recommendations(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        """正常返回推荐列表。"""
        items = [_make_recommend_item(), _make_recommend_item()]
        mock_result = [
            {"doc_id": i["doc_id"], "title": i["title"], "reason": i["reason"], "score": i["score"]}
            for i in items
        ]

        with patch("app.api.v1.recommendations.RecommendationService") as svc_cls:
            svc_cls.return_value.recommend_for_user = AsyncMock(return_value=mock_result)
            svc_cls.return_value.tenant_id = None
            with patch("app.api.v1.recommendations.PermissionService") as perm_cls:
                perm_cls.return_value.filter_documents = AsyncMock(
                    return_value=[]
                )
                response = await auth_client.get(
                    "/api/v1/recommendations/user", params={"top_k": 5}
                )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert len(data["data"]) == 2
        assert data["data"][0]["doc_id"] == str(items[0]["doc_id"])
        assert data["data"][0]["reason"] == "hot"

    @pytest.mark.asyncio
    async def test_top_k_validation(self, auth_client: httpx.AsyncClient) -> None:
        """top_k 超出范围应返回 422。"""
        with patch("app.api.v1.recommendations.RecommendationService"):
            with patch("app.api.v1.recommendations.PermissionService"):
                response = await auth_client.get(
                    "/api/v1/recommendations/user", params={"top_k": 100}
                )
        assert response.status_code == 422


# ======================================================================
# 相关阅读
# ======================================================================


class TestRelatedDocuments:
    """GET /api/v1/recommendations/document/{doc_id} 测试。"""

    @pytest.mark.asyncio
    async def test_returns_related(self, auth_client: httpx.AsyncClient) -> None:
        """正常返回相关阅读。"""
        doc_id = uuid4()
        mock_result = [_make_recommend_item(doc_id)]

        with patch("app.api.v1.recommendations.RecommendationService") as svc_cls:
            svc_cls.return_value.get_related_documents = AsyncMock(
                return_value=mock_result
            )
            with patch("app.api.v1.recommendations.PermissionService") as perm_cls:
                perm_cls.return_value.filter_documents = AsyncMock(return_value=[])
                response = await auth_client.get(
                    f"/api/v1/recommendations/document/{doc_id}"
                )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"][0]["doc_id"] == str(doc_id)

    @pytest.mark.asyncio
    async def test_invalid_doc_id(self, auth_client: httpx.AsyncClient) -> None:
        """非法文档 ID 应返回 400。"""
        with patch("app.api.v1.recommendations.RecommendationService"):
            with patch("app.api.v1.recommendations.PermissionService"):
                response = await auth_client.get(
                    "/api/v1/recommendations/document/not-a-uuid"
                )
        assert response.status_code == 200
        assert response.json()["code"] == 400


# ======================================================================
# 行为上报
# ======================================================================


class TestFeedback:
    """POST /api/v1/recommendations/feedback 测试。"""

    @pytest.mark.asyncio
    async def test_report_behavior(self, auth_client: httpx.AsyncClient) -> None:
        """正常上报行为。"""
        doc_id = uuid4()

        with patch("app.api.v1.recommendations.RecommendationService") as svc_cls:
            svc_cls.return_value.record_behavior = AsyncMock()
            response = await auth_client.post(
                "/api/v1/recommendations/feedback",
                json={"doc_id": str(doc_id), "action_type": "view"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["status"] == "ok"
        svc_cls.return_value.record_behavior.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_action(self, auth_client: httpx.AsyncClient) -> None:
        """非法行为类型应返回 422。"""
        with patch("app.api.v1.recommendations.RecommendationService"):
            response = await auth_client.post(
                "/api/v1/recommendations/feedback",
                json={"doc_id": str(uuid4()), "action_type": "hack"},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_doc_id(self, auth_client: httpx.AsyncClient) -> None:
        """非法文档 ID 应返回 400。"""
        with patch("app.api.v1.recommendations.RecommendationService"):
            response = await auth_client.post(
                "/api/v1/recommendations/feedback",
                json={"doc_id": "not-a-uuid", "action_type": "view"},
            )
        assert response.status_code == 200
        assert response.json()["code"] == 400


# ======================================================================
# 重建端点
# ======================================================================


class TestRebuild:
    """POST /api/v1/recommendations/rebuild 测试。"""

    @pytest.mark.asyncio
    async def test_rebuild_requires_admin(self, auth_client: httpx.AsyncClient) -> None:
        """非管理员调用重建应返回 403。"""
        with patch("app.api.v1.recommendations.RecommendationService"):
            response = await auth_client.post("/api/v1/recommendations/rebuild")
        assert response.status_code == 200
        assert response.json()["code"] == 403

    @pytest.mark.asyncio
    async def test_rebuild_admin(self) -> None:
        """管理员调用重建应返回 queued。"""
        from app.database import get_db_session
        from app.deps import get_current_user
        from app.main import app
        from app.middleware import get_rate_limiter

        limiter = get_rate_limiter()
        if limiter is not None:
            limiter.clear()

        mock_user = _make_user(role="admin")

        async def override_user():
            return mock_user

        async def override_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[get_db_session] = override_db

        # 端点现已提交真实 Celery 任务 — mock delay 避免依赖 broker
        mock_task = AsyncMock()
        mock_task.delay = lambda **kwargs: SimpleNamespace(id="celery-task-test")

        with patch("app.api.v1.recommendations.RecommendationService"), \
             patch("tasks.recommendation_tasks.rebuild_recommendation_model", mock_task):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post("/api/v1/recommendations/rebuild")

        app.dependency_overrides.clear()
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["status"] == "queued"
        assert data["data"]["task_id"] == "celery-task-test"