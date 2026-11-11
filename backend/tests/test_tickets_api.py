"""IT 工单 API 回归测试（任务 #5）。

背景：``frontend/src/pages/scenes/it-helpdesk.astro`` + ``lib/apis/tickets.ts``
一直在调 ``/api/v1/tickets``（列表 / 创建 / 更新），但后端**整个模块不存在**
（无 router / model / service / 迁移），场景页一进去就是"工单列表加载失败"。

本文件锁住前端依赖的三条契约：
    GET  /api/v1/tickets           → ``{items, total, page, size, pages}``
    POST /api/v1/tickets           → 201 + 工单对象（含 id，页面随后用它改状态）
    PUT  /api/v1/tickets/{id}      → 200，支持 ``{status: 'resolved', ai_solved: true}``

外加状态机、租户隔离与软删除。

测试用独立 mini app（只挂 tickets 路由 + 注入 tenant_id 的中间件），
避免往全局 ``app`` 上动态加中间件污染其它用例。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI


@pytest_asyncio.fixture
async def seeded(db_session):
    """建两个不同租户的用户，用于验证租户隔离。

    必须先建 Tenant 行：``users.tenant_id`` 有真实外键
    ``fk_users_tenant_id``，直接塞随机 UUID 会 ForeignKeyViolation。
    """
    from app.models.billing import Tenant
    from app.models.user import User

    tenant_a = Tenant(name="租户 A", domain="a.ticket.local")
    tenant_b = Tenant(name="租户 B", domain="b.ticket.local")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()

    alice = User(
        email="alice@ticket.local",
        hashed_password="not-used",
        name="Alice",
        role="editor",
        clearance_level="internal",
        tenant_id=tenant_a.id,
    )
    bob = User(
        email="bob@ticket.local",
        hashed_password="not-used",
        name="Bob",
        role="editor",
        clearance_level="internal",
        tenant_id=tenant_b.id,
    )
    db_session.add_all([alice, bob])
    await db_session.commit()
    return SimpleNamespace(
        alice=alice, bob=bob, tenant_a=tenant_a.id, tenant_b=tenant_b.id
    )


def _build_app(session, user, tenant_id) -> FastAPI:
    """构造仅含工单路由的测试应用（真实 PG session + 固定用户 + 固定租户）。"""
    from app.api.v1.tickets import router as tickets_router
    from app.database import get_db_session
    from app.deps import get_current_active_user

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request, call_next):
        # 等价于生产环境的 TenantContextMiddleware：从 JWT 解出租户写进 state
        request.state.tenant_id = tenant_id
        return await call_next(request)

    app.include_router(tickets_router, prefix="/api/v1")

    async def override_user():
        return user

    async def override_db():
        yield session

    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_db_session] = override_db
    return app


@asynccontextmanager
async def _client_as(session, user, tenant_id=None):
    app = _build_app(session, user, tenant_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ticket-test",
    ) as client:
        yield client


# ======================================================================
# 创建
# ======================================================================


@pytest.mark.asyncio
async def test_create_ticket(db_session, seeded) -> None:
    """POST 返回 201 + 工单对象，状态初始为 open，提交人快照落库。"""
    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        resp = await client.post(
            "/api/v1/tickets",
            json={
                "title": "VPN 连不上",
                "description": "在家无法连接公司 VPN，提示认证失败",
                "category": "network",
                "priority": "high",
            },
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["id"]
    assert data["status"] == "open"
    assert data["category"] == "network"
    assert data["priority"] == "high"
    assert data["user_name"] == "Alice"


# ======================================================================
# 列表（前端 getTickets 的返回结构）
# ======================================================================


@pytest.mark.asyncio
async def test_list_tickets_shape_and_filters(db_session, seeded) -> None:
    """GET 必须返回 {items, total, page, size, pages}，并支持 status/category 过滤。"""
    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        for title, category in (("打印机坏了", "hardware"), ("邮箱容量不足", "software")):
            created = await client.post(
                "/api/v1/tickets",
                json={"title": title, "description": "描述", "category": category},
            )
            assert created.status_code == 201, created.text

        listing = await client.get("/api/v1/tickets")
        assert listing.status_code == 200, listing.text
        body = listing.json()["data"]
        for key in ("items", "total", "page", "size", "pages"):
            assert key in body, f"前端 PageResponse 需要 {key}"
        assert body["total"] == 2
        assert len(body["items"]) == 2

        by_category = await client.get("/api/v1/tickets", params={"category": "hardware"})
        assert by_category.json()["data"]["total"] == 1
        assert by_category.json()["data"]["items"][0]["title"] == "打印机坏了"

        by_status = await client.get("/api/v1/tickets", params={"status": "resolved"})
        assert by_status.json()["data"]["total"] == 0

        bad_status = await client.get("/api/v1/tickets", params={"status": "weird"})
        assert bad_status.status_code == 422


@pytest.mark.asyncio
async def test_mine_filter(db_session, seeded) -> None:
    """``mine=true`` 只返回当前用户提交的工单。"""
    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        await client.post(
            "/api/v1/tickets",
            json={"title": "Alice 的工单", "description": "x"},
        )
    async with _client_as(db_session, seeded.bob, seeded.tenant_b) as client:
        await client.post(
            "/api/v1/tickets",
            json={"title": "Bob 的工单", "description": "y"},
        )
        listing = await client.get("/api/v1/tickets", params={"mine": "true"})
    titles = [t["title"] for t in listing.json()["data"]["items"]]
    assert titles == ["Bob 的工单"]


# ======================================================================
# 状态机
# ======================================================================


@pytest.mark.asyncio
async def test_update_status_transitions(db_session, seeded) -> None:
    """open→progress→resolved 合法；resolved→open 必须 409（终态不可回退）。"""
    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        created = await client.post(
            "/api/v1/tickets",
            json={"title": "需要重置密码", "description": "忘记密码"},
        )
        ticket_id = created.json()["data"]["id"]

        to_progress = await client.put(
            f"/api/v1/tickets/{ticket_id}", json={"status": "progress"}
        )
        assert to_progress.status_code == 200, to_progress.text
        assert to_progress.json()["data"]["status"] == "progress"

        to_resolved = await client.put(
            f"/api/v1/tickets/{ticket_id}",
            json={"status": "resolved", "ai_solved": True, "ai_answer": "已重置"},
        )
        assert to_resolved.status_code == 200, to_resolved.text
        assert to_resolved.json()["data"]["ai_solved"] is True

        reopen = await client.put(
            f"/api/v1/tickets/{ticket_id}", json={"status": "open"}
        )
        assert reopen.status_code == 409, reopen.text
        assert "非法状态流转" in reopen.text


@pytest.mark.asyncio
async def test_resolve_by_ai_endpoint(db_session, seeded) -> None:
    """显式「采纳 AI 解答并关闭」端点：open 可直接 resolved。"""
    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        created = await client.post(
            "/api/v1/tickets", json={"title": "如何申请软件许可", "description": "?"}
        )
        ticket_id = created.json()["data"]["id"]
        resp = await client.post(
            f"/api/v1/tickets/{ticket_id}/resolve-by-ai",
            params={"ai_answer": "在 IT 门户提交许可申请", "ai_solution": "1. 打开门户"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "resolved"
    assert data["ai_solved"] is True
    assert data["ai_answer"].startswith("在 IT 门户")


@pytest.mark.asyncio
async def test_update_nonexistent_ticket_404(db_session, seeded) -> None:
    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        resp = await client.put(
            f"/api/v1/tickets/{uuid.uuid4()}", json={"status": "progress"}
        )
    assert resp.status_code == 404, resp.text


# ======================================================================
# 租户隔离
# ======================================================================


@pytest.mark.asyncio
async def test_tenant_isolation(db_session, seeded) -> None:
    """A 租户看不到也改不了 B 租户的工单。"""
    async with _client_as(db_session, seeded.bob, seeded.tenant_b) as client:
        created = await client.post(
            "/api/v1/tickets", json={"title": "Bob 私有工单", "description": "x"}
        )
        bob_ticket = created.json()["data"]["id"]

    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        listing = await client.get("/api/v1/tickets")
        assert all(t["title"] != "Bob 私有工单" for t in listing.json()["data"]["items"])

        detail = await client.get(f"/api/v1/tickets/{bob_ticket}")
        assert detail.status_code == 404, detail.text

        hijack = await client.put(
            f"/api/v1/tickets/{bob_ticket}", json={"status": "resolved"}
        )
        assert hijack.status_code == 404, hijack.text


# ======================================================================
# 软删除
# ======================================================================


@pytest.mark.asyncio
async def test_soft_delete_hides_ticket(db_session, seeded) -> None:
    async with _client_as(db_session, seeded.alice, seeded.tenant_a) as client:
        created = await client.post(
            "/api/v1/tickets", json={"title": "待删除", "description": "x"}
        )
        ticket_id = created.json()["data"]["id"]
        deleted = await client.delete(f"/api/v1/tickets/{ticket_id}")
        assert deleted.status_code == 200, deleted.text

        listing = await client.get("/api/v1/tickets")
        assert listing.json()["data"]["total"] == 0

        detail = await client.get(f"/api/v1/tickets/{ticket_id}")
        assert detail.status_code == 404

    # 库里是软删除，不是物理删除
    from sqlalchemy import select

    from app.models.ticket import Ticket

    row = (
        await db_session.execute(select(Ticket).where(Ticket.id == uuid.UUID(ticket_id)))
    ).scalars().one()
    assert row.deleted_at is not None


# ======================================================================
# 权限
# ======================================================================


@pytest.mark.asyncio
async def test_requires_authentication() -> None:
    """真实应用里未登录请求必须 401/403（工单含个人隐私，不能公开）。"""
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ticket-test"
    ) as client:
        resp = await client.get("/api/v1/tickets")
    assert resp.status_code in (401, 403), resp.text
