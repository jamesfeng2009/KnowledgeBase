"""Deep Research API 测试 — 触发课题调研长任务 + tenant_id 透传。

覆盖：
    - 认证强制：未携带 token 返回 401；
    - POST /api/v1/research：以 job.id 为 task_id 派发 deep_research_task，
      返回真实 task_id；
    - 租户透传：请求带 X-Tenant-Id 时，apply_async 收到的 tenant_id=该值；
      不带时收到 None（全局 scope）。
    - P1 权限收敛：kb_ids 与可访问集合取交集 / 收敛失败 fail-closed 置空。

幂等创建的完整行为（并发重试/冲突）见 test_research_idempotency.py（真库）。
mock 风格参照 test_recommendation_api.py（httpx ASGI + dependency_overrides）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
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
    import app.middleware as _mw

    # 重置两级限流器全局 — 本文件测试认证/派发，不禁用会被
    # test_rate_limiter 等前置测试打满的桶污染（429 / MagicMock TypeError）
    _mw._rate_limiter = None
    _mw._tenant_rate_limiter = None
    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client():
    """带认证覆盖的客户端（db 为 AsyncMock — 本文件只测派发机制）。"""
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app
    import app.middleware as _mw

    # 同 raw_client — 隔离前置测试残留的限流桶
    _mw._rate_limiter = None
    _mw._tenant_rate_limiter = None

    mock_user = _make_user()

    async def override_user():
        return mock_user

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()


def _patch_dispatch():
    """Mock deep_research_task.apply_async（task_id 由端点显式指定）。"""
    return patch(
        "tasks.deep_research_tasks.deep_research_task.apply_async",
        return_value=SimpleNamespace(id="unused"),
    )


def _patch_perm(accessible):
    """Mock PermissionService — get_accessible_kb_ids 返回 accessible。

    accessible=None 表示不限制（admin）；集合表示普通用户可访问范围；
    抛异常模拟权限服务故障（端点应 fail-closed）。
    """
    svc = MagicMock()
    svc.get_accessible_kb_ids = AsyncMock(return_value=accessible)
    return patch(
        "app.services.permission_service.PermissionService",
        return_value=svc,
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
        """落 research_jobs 记录并以 job.id 为 task_id 派发（mock db 不落库）。"""
        with _patch_dispatch() as mock_dispatch, _patch_perm(None):  # None=不限制
            resp = await auth_client.post(
                "/api/v1/research", json={"goal": "调研某主题", "kb_ids": ["kb1"]}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["status"] == "queued"
        assert body["data"]["reused"] is False
        # apply_async 收到 (goal, kb_ids) + tenant_id=None + 显式 task_id
        call = mock_dispatch.call_args
        assert call.kwargs["args"] == ["调研某主题", ["kb1"]]
        assert call.kwargs["kwargs"] == {"tenant_id": None}
        assert call.kwargs["task_id"] == body["data"]["task_id"]

    @pytest.mark.asyncio
    async def test_kb_ids_intersected_with_accessible(self, auth_client) -> None:
        """P1 权限收敛 — 客户端 kb_ids 与可访问集合取交集后派发。"""
        kb_allowed, kb_denied = str(uuid4()), str(uuid4())
        with _patch_dispatch() as mock_dispatch, _patch_perm({kb_allowed}):
            resp = await auth_client.post(
                "/api/v1/research",
                json={"goal": "调研某主题", "kb_ids": [kb_allowed, kb_denied]},
            )
        assert resp.status_code == 200
        call = mock_dispatch.call_args
        assert call.kwargs["args"] == ["调研某主题", [kb_allowed]]

    @pytest.mark.asyncio
    async def test_none_kb_ids_expanded_to_accessible(self, auth_client) -> None:
        """kb_ids=None（全部）时展开为可访问集合（worker 无用户上下文）。"""
        kb_a, kb_b = str(uuid4()), str(uuid4())
        with _patch_dispatch() as mock_dispatch, _patch_perm({kb_a, kb_b}):
            resp = await auth_client.post(
                "/api/v1/research", json={"goal": "调研某主题"}
            )
        assert resp.status_code == 200
        call = mock_dispatch.call_args
        assert call.kwargs["args"] == ["调研某主题", sorted([kb_a, kb_b])]

    @pytest.mark.asyncio
    async def test_perm_failure_fails_closed(self, auth_client) -> None:
        """权限收敛异常 — fail-closed：kb_ids 置空，不回落为客户端原值。"""
        svc = MagicMock()
        svc.get_accessible_kb_ids = AsyncMock(side_effect=RuntimeError("db down"))
        with _patch_dispatch() as mock_dispatch, patch(
            "app.services.permission_service.PermissionService", return_value=svc
        ):
            resp = await auth_client.post(
                "/api/v1/research", json={"goal": "调研某主题", "kb_ids": ["kb1"]}
            )
        assert resp.status_code == 200
        call = mock_dispatch.call_args
        assert call.kwargs["args"] == ["调研某主题", []]

    @pytest.mark.asyncio
    async def test_passes_tenant_id_from_request_state(self, auth_client) -> None:
        """请求带 X-Tenant-Id（合法 UUID）时，tenant_id 透传给任务。"""
        tid = str(uuid4())
        with _patch_dispatch() as mock_dispatch, _patch_perm(None):
            resp = await auth_client.post(
                "/api/v1/research",
                json={"goal": "调研某主题"},
                headers={"X-Tenant-Id": tid},
            )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        call = mock_dispatch.call_args
        assert call.kwargs["kwargs"] == {"tenant_id": tid}

    @pytest.mark.asyncio
    async def test_goal_validation(self, auth_client) -> None:
        """goal 过短应返回 422（pydantic 校验）。"""
        resp = await auth_client.post("/api/v1/research", json={"goal": "x"})
        assert resp.status_code == 422
