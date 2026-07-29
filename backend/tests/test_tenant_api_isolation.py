"""
租户模块管理 API 跨租户隔离测试 — 针对 tenants.py 三端点的安全回归。

历史问题：GET/PUT /tenants/modules 与 PATCH /tenants/modules/{id}
调用 TenantService 时未传 tenant_id，tenant_id=None 会回落到
"第一条活跃租户"，导致 SaaS 模式下任何登录用户读到/修改的都是
同一个租户的模块配置（跨租户数据泄漏 + 跨租户写入）。

本测试在 API 层验证：
1. GET /tenants/modules 返回当前用户所属租户的配置
2. PUT /tenants/modules 只修改当前租户，不影响其他租户
3. PATCH /tenants/modules/{module_id} 只开关当前租户的模块
4. 非 admin 用户无法修改模块配置（403）

依赖 PostgreSQL 数据库（与 test_tenant_module_gating.py 相同环境）。
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.settings import router as settings_router
from app.api.v1.tenants import router as tenants_router
from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.base import Base
from app.models.billing import Tenant
from app.models.user import User
from app.services.tenant_service import TenantService


# ==================================================================
# Fixtures
# ==================================================================


@pytest_asyncio.fixture
async def db_session():
    """创建 PostgreSQL 数据库用于测试（与全局 conftest 相同模式）。"""
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    session = async_session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_a(db_session):
    """租户 A — pro 套餐，当前用户所属租户。"""
    t = Tenant(name="租户A", plan="pro", max_users=50, max_storage=1073741824)
    db_session.add(t)
    await db_session.flush()
    return t


@pytest_asyncio.fixture
async def tenant_b(db_session):
    """租户 B — enterprise 套餐，用于验证不被跨租户操作影响。

    注意：必须先创建租户 B 再创建租户 A 的场景也要覆盖，
    因为原 bug 中 tenant_id=None 会命中"第一条活跃租户"。
    """
    t = Tenant(
        name="租户B",
        plan="enterprise",
        max_users=200,
        max_storage=10737418240,
        settings={"enabled_modules": ["knowledge_base", "multimodal"]},
    )
    db_session.add(t)
    await db_session.flush()
    return t


def _build_app(db_session: AsyncSession, user: User) -> FastAPI:
    """构建挂载 tenants/settings 路由的测试应用，覆盖 DB 与认证依赖。"""
    app = FastAPI()
    app.include_router(tenants_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")

    async def override_db():
        yield db_session

    async def override_user():
        return user

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_current_active_user] = override_user
    return app


def _make_user(tenant_id, role: str = "admin") -> User:
    """构造属于指定租户的用户。"""
    u = User(
        email=f"test-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="fake",
        name="测试用户",
        role=role,
        is_active=True,
    )
    u.id = uuid.uuid4()
    u.tenant_id = tenant_id
    return u


# ==================================================================
# 1. GET /tenants/modules — 读取隔离
# ==================================================================


class TestGetTenantModulesIsolation:
    """GET /tenants/modules 必须返回当前租户的配置。"""

    async def test_returns_current_tenant_config(
        self, db_session, tenant_b, tenant_a
    ):
        """租户 B 先创建（成为"第一条活跃租户"），用户属于租户 A。

        修复前：tenant_id=None 命中租户 B，用户看到 B 的配置。
        修复后：必须返回租户 A（pro 套餐）的模块状态。
        """
        user = _make_user(tenant_a.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/tenants/modules")

        assert resp.status_code == 200
        modules = resp.json()["data"]
        # pro 套餐含 doc_intelligence，不含 multimodal
        doc_intel = next(m for m in modules if m["id"] == "doc_intelligence")
        assert doc_intel["enabled"] is True
        multimodal = next(m for m in modules if m["id"] == "multimodal")
        # 关键断言：若错误地命中租户 B（settings 含 multimodal），
        # 此处会错误地为 True
        assert multimodal["enabled"] is False


# ==================================================================
# 2. PUT /tenants/modules — 写入隔离
# ==================================================================


class TestUpdateTenantModulesIsolation:
    """PUT /tenants/modules 必须只修改当前租户。"""

    async def test_update_only_affects_current_tenant(
        self, db_session, tenant_b, tenant_a
    ):
        """更新租户 A 的模块，租户 B 的 settings 必须保持不变。"""
        user = _make_user(tenant_a.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/tenants/modules",
                json={"module_ids": ["doc_intelligence", "multimodal"]},
            )

        assert resp.status_code == 200
        enabled = resp.json()["data"]["enabled_modules"]
        assert "multimodal" in enabled
        assert "doc_intelligence" in enabled

        # 验证租户 A 已更新
        await db_session.refresh(tenant_a)
        assert "multimodal" in tenant_a.settings["enabled_modules"]

        # 关键断言：租户 B 的 settings 未被污染
        await db_session.refresh(tenant_b)
        assert tenant_b.settings["enabled_modules"] == [
            "knowledge_base",
            "multimodal",
        ]

    async def test_non_admin_forbidden(self, db_session, tenant_a):
        """非 admin 用户无权修改模块配置（403）。"""
        user = _make_user(tenant_a.id, role="member")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/tenants/modules",
                json={"module_ids": ["doc_intelligence"]},
            )

        assert resp.status_code == 403

    async def test_invalid_module_id_returns_400(
        self, db_session, tenant_a
    ):
        """传入未注册模块 ID 返回 400。"""
        user = _make_user(tenant_a.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/tenants/modules",
                json={"module_ids": ["nonexistent_module"]},
            )

        assert resp.status_code == 400


# ==================================================================
# 3. PATCH /tenants/modules/{module_id} — 开关隔离
# ==================================================================


class TestToggleTenantModuleIsolation:
    """PATCH /tenants/modules/{module_id} 必须只开关当前租户的模块。"""

    async def test_toggle_only_affects_current_tenant(
        self, db_session, tenant_b, tenant_a
    ):
        """为租户 A 开启 multimodal，租户 B 的 settings 不变。"""
        user = _make_user(tenant_a.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/tenants/modules/multimodal",
                json={"enabled": True},
            )

        assert resp.status_code == 200
        assert "multimodal" in resp.json()["data"]["enabled_modules"]

        # 租户 A 已开启
        await db_session.refresh(tenant_a)
        assert "multimodal" in tenant_a.settings["enabled_modules"]

        # 关键断言：租户 B 未被污染
        await db_session.refresh(tenant_b)
        assert tenant_b.settings["enabled_modules"] == [
            "knowledge_base",
            "multimodal",
        ]

    async def test_disable_module_for_current_tenant(
        self, db_session, tenant_b
    ):
        """租户 B 用户关闭 multimodal，只影响租户 B 自身。"""
        user = _make_user(tenant_b.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/tenants/modules/multimodal",
                json={"enabled": False},
            )

        assert resp.status_code == 200
        assert "multimodal" not in resp.json()["data"]["enabled_modules"]

        await db_session.refresh(tenant_b)
        assert "multimodal" not in tenant_b.settings["enabled_modules"]

    async def test_toggle_basic_module_ignored(
        self, db_session, tenant_a
    ):
        """基础模块不可关闭（静默忽略）。"""
        user = _make_user(tenant_a.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/tenants/modules/knowledge_base",
                json={"enabled": False},
            )

        assert resp.status_code == 200
        # 基础模块仍在启用列表中
        assert "knowledge_base" in resp.json()["data"]["enabled_modules"]


# ==================================================================
# 4. Service 层交叉验证 — tenant_id 传递链路
# ==================================================================


class TestServiceTenantPropagation:
    """验证 API 修复后 Service 层确实收到了正确的 tenant_id。"""

    async def test_service_reads_correct_tenant(
        self, db_session, tenant_b, tenant_a
    ):
        """直接以 tenant_a.id 调用 Service，读取结果属于租户 A。"""
        service = TenantService(db_session)
        modules = await service.list_modules_with_status(
            tenant_id=tenant_a.id
        )
        multimodal = next(m for m in modules if m["id"] == "multimodal")
        # 租户 A 是 pro 套餐，不含 multimodal
        assert multimodal["enabled"] is False

        # 对照：租户 B（settings 显式含 multimodal）
        modules_b = await service.list_modules_with_status(
            tenant_id=tenant_b.id
        )
        multimodal_b = next(
            m for m in modules_b if m["id"] == "multimodal"
        )
        assert multimodal_b["enabled"] is True


# ==================================================================
# 5. JSONB 持久化回归 — settings.py / tenant_service.py 写入必须落库
# ==================================================================
#
# 历史问题：settings 是裸 JSONB 列（未用 MutableDict），原实现
# 原地突变同一 dict 对象再赋值回去，SQLAlchemy 对相同对象赋值
# 跳过变更检测，flush 时 UPDATE 静默丢失 — API 返回成功但
# 重新读取时配置从未变化。


class TestJsonbWritePersistence:
    """对已有 settings 的租户执行写操作，必须真实持久化到 DB。"""

    async def test_toggle_module_persists_to_db(
        self, db_session, tenant_b
    ):
        """关闭模块后 refresh 读到的必须是新值（Service 层修复验证）。"""
        service = TenantService(db_session)
        result = await service.toggle_module(
            "multimodal", False, tenant_id=tenant_b.id
        )
        assert "multimodal" not in result
        await db_session.commit()

        await db_session.refresh(tenant_b)
        assert "multimodal" not in tenant_b.settings["enabled_modules"]

    async def test_update_llm_config_persists_to_db(
        self, db_session, tenant_b
    ):
        """PUT /settings/llm 修改 model 后必须落库。"""
        user = _make_user(tenant_b.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/settings/llm",
                json={"model": "Qwen/Qwen3-72B-Instruct"},
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["model"] == "Qwen/Qwen3-72B-Instruct"

        await db_session.commit()
        await db_session.refresh(tenant_b)
        assert (
            tenant_b.settings["llm_config"]["model"]
            == "Qwen/Qwen3-72B-Instruct"
        )
        # 原 enabled_modules 键必须保留（不破坏其他配置）
        assert "enabled_modules" in tenant_b.settings

    async def test_update_system_config_persists_to_db(
        self, db_session, tenant_b
    ):
        """PUT /settings/system 修改 site_name 后必须落库。"""
        user = _make_user(tenant_b.id, role="admin")
        app = _build_app(db_session, user)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/settings/system",
                json={"site_name": "企业知识大脑-测试"},
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["site_name"] == "企业知识大脑-测试"

        await db_session.commit()
        await db_session.refresh(tenant_b)
        assert (
            tenant_b.settings["system_config"]["site_name"]
            == "企业知识大脑-测试"
        )
        # 原 enabled_modules 键必须保留
        assert tenant_b.settings["enabled_modules"] == [
            "knowledge_base",
            "multimodal",
        ]
