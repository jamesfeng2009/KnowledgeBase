"""Deep Research API 测试 — 触发课题调研长任务 + tenant_id 透传。

覆盖：
    - 认证强制：未携带 token 返回 401；
    - POST /api/v1/research：派发 deep_research_task.delay，返回真实 task_id；
    - 租户透传：请求带 X-Tenant-Id 时，delay 收到的 tenant_id=该值；
      不带时收到 None（全局 scope）。

mock 风格参照 test_recommendation_api.py（httpx ASGI + dependency_overrides）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio


def _make_user(role: str = "editor") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), role=role, is_active=True, email="u@test.com", name="测试用户",
    )


@pytest_asyncio.fixture
async def raw_client():
    """无认证覆盖的客户端 — 用于测试认证强制。"""
    from app.main import app
    from app.middleware import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()
    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client():
    """带认证覆盖的客户端。"""
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app
    from app.middleware import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    mock_user = _make_user()

    async def override_user():
        return mock_user

    async def override_db():
        yield None

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()


def _patch_delay():
    """Mock deep_research_task.delay，返回伪造 task_id。"""
    return patch(
        "tasks.deep_research_tasks.deep_research_task.delay",
        return_value=SimpleNamespace(id="fake-task-id"),
    )


class TestResearchAuth:
    @pytest.mark.asyncio
    async def test_requires_auth(self, raw_client) -> None:
        """未携带 token 访问 POST /research 应返回 401。"""
        resp = await raw_client.post(
            "/api/v1/research", json={"goal": "调研某主题"}
        )
        assert resp.status_code == 401


class TestResearchDispatch:
    @pytest.mark.asyncio
    async def test_submits_task_and_returns_task_id(self, auth_client) -> None:
        """派发任务并返回 task_id（依赖注入真实调用 delay）。"""
        with _patch_delay() as mock_delay:
            resp = await auth_client.post(
                "/api/v1/research", json={"goal": "调研某主题", "kb_ids": ["kb1"]}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["status"] == "queued"
        assert body["data"]["task_id"] == "fake-task-id"
        # delay 收到 (goal, kb_ids, tenant_id=None) —— 无 X-Tenant-Id 头时全局 scope
        mock_delay.assert_called_once_with(
            "调研某主题", ["kb1"], tenant_id=None
        )

    @pytest.mark.asyncio
    async def test_passes_tenant_id_from_request_state(self, auth_client) -> None:
        """请求带 X-Tenant-Id（合法 UUID）时，tenant_id 透传给 delay。"""
        tid = str(uuid4())
        with _patch_delay() as mock_delay:
            resp = await auth_client.post(
                "/api/v1/research",
                json={"goal": "调研某主题"},
                headers={"X-Tenant-Id": tid},
            )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        mock_delay.assert_called_once_with(
            "调研某主题", None, tenant_id=tid
        )

    @pytest.mark.asyncio
    async def test_goal_validation(self, auth_client) -> None:
        """goal 过短应返回 422（pydantic 校验）。"""
        resp = await auth_client.post("/api/v1/research", json={"goal": "x"})
        assert resp.status_code == 422