"""
多租户模块门控测试 — 测试模块注册表、TenantService、require_module 依赖。

覆盖场景：
1. 模块注册表完整性（MODULE_REGISTRY / 套餐默认 / 便捷查询函数）
2. TenantService 读写（获取启用模块、更新、开关、套餐默认回退）
3. require_module 依赖注入（基础模块通过、可选模块门控、租户不存在兜底）

不依赖外部服务，使用 SQLite 内存数据库。
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.modules import (
    BASIC_MODULE_IDS,
    MODULE_IDS,
    MODULE_REGISTRY,
    OPTIONAL_MODULE_IDS,
    PLAN_DEFAULTS,
    get_module_info,
    is_valid_module,
    merge_with_basics,
)
from app.database import get_db_session
from app.deps import require_module
from app.models.base import Base
from app.models.billing import Tenant
from app.models.user import User
from app.services.tenant_service import TenantService


# ==================================================================
# 1. 模块注册表测试
# ==================================================================


class TestModuleRegistry:
    """模块注册表 — 验证模块定义完整性和便捷查询函数。"""

    def test_registry_not_empty(self):
        """注册表非空。"""
        assert len(MODULE_REGISTRY) >= 10

    def test_all_modules_have_required_fields(self):
        """每个模块必须包含 id/name/description/category/is_basic。"""
        for m in MODULE_REGISTRY:
            assert m.id, f"模块 id 为空: {m}"
            assert m.name, f"模块 name 为空: {m}"
            assert m.description, f"模块 description 为空: {m}"
            assert m.category in ("basic", "intelligence", "integration"), (
                f"模块 {m.id} category 无效: {m.category}"
            )
            assert isinstance(m.is_basic, bool)

    def test_module_ids_unique(self):
        """模块 ID 不可重复。"""
        ids = [m.id for m in MODULE_REGISTRY]
        assert len(ids) == len(set(ids)), "存在重复的模块 ID"

    def test_basic_modules_exist(self):
        """至少有 3 个基础模块。"""
        basics = {m.id for m in MODULE_REGISTRY if m.is_basic}
        assert "knowledge_base" in basics
        assert "audit_workflow" in basics
        assert "qa_community" in basics
        assert len(basics) >= 3

    def test_optional_modules_exist(self):
        """至少有 7 个可选模块。"""
        optionals = {m.id for m in MODULE_REGISTRY if not m.is_basic}
        assert "doc_intelligence" in optionals
        assert "analytics_dashboard" in optionals
        assert "knowledge_graph" in optionals
        assert "expert_discovery" in optionals
        assert "knowledge_push" in optionals
        assert "unified_search" in optionals
        assert "multimodal" in optionals
        assert len(optionals) >= 7

    def test_basic_and_optional_partition(self):
        """基础模块与可选模块互斥且并集为全部。"""
        assert BASIC_MODULE_IDS.isdisjoint(OPTIONAL_MODULE_IDS)
        assert BASIC_MODULE_IDS | OPTIONAL_MODULE_IDS == MODULE_IDS

    def test_plan_defaults_include_basics(self):
        """所有套餐默认模块都包含基础模块。"""
        for plan, modules in PLAN_DEFAULTS.items():
            assert BASIC_MODULE_IDS.issubset(set(modules)), (
                f"套餐 {plan} 缺少基础模块: {BASIC_MODULE_IDS - set(modules)}"
            )

    def test_enterprise_plan_has_all(self):
        """enterprise 套餐包含所有模块。"""
        assert set(PLAN_DEFAULTS["enterprise"]) == MODULE_IDS

    def test_free_plan_only_basics(self):
        """free 套餐仅含基础模块。"""
        assert set(PLAN_DEFAULTS["free"]) == BASIC_MODULE_IDS

    def test_pro_plan_is_superset_of_free(self):
        """pro 套餐是 free 的超集。"""
        assert set(PLAN_DEFAULTS["free"]).issubset(PLAN_DEFAULTS["pro"])

    def test_get_module_info(self):
        """get_module_info 返回正确的模块定义。"""
        info = get_module_info("doc_intelligence")
        assert info is not None
        assert info.id == "doc_intelligence"
        assert info.name == "文档智能处理"

    def test_get_module_info_not_found(self):
        """get_module_info 对未知 ID 返回 None。"""
        assert get_module_info("nonexistent") is None

    def test_is_valid_module(self):
        """is_valid_module 正确识别有效和无效模块。"""
        assert is_valid_module("knowledge_base")
        assert is_valid_module("multimodal")
        assert not is_valid_module("nonexistent")

    def test_merge_with_basics(self):
        """merge_with_basics 自动补齐基础模块。"""
        result = merge_with_basics(["doc_intelligence"])
        assert "knowledge_base" in result
        assert "audit_workflow" in result
        assert "qa_community" in result
        assert "doc_intelligence" in result


# ==================================================================
# 2. TenantService 测试
# ==================================================================


@pytest_asyncio.fixture
async def db_session():
    """创建 SQLite 内存数据库用于测试。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = async_session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_free(db_session):
    """创建 free 套餐租户。"""
    t = Tenant(name="免费租户", plan="free", max_users=5, max_storage=1024)
    db_session.add(t)
    await db_session.flush()
    return t


@pytest_asyncio.fixture
async def tenant_pro(db_session):
    """创建 pro 套餐租户。"""
    t = Tenant(name="专业租户", plan="pro", max_users=50, max_storage=1073741824)
    db_session.add(t)
    await db_session.flush()
    return t


@pytest_asyncio.fixture
async def tenant_enterprise_with_settings(db_session):
    """创建 enterprise 租户并预置 settings。"""
    t = Tenant(
        name="企业租户",
        plan="enterprise",
        max_users=200,
        max_storage=10737418240,
        settings={"enabled_modules": ["knowledge_base", "multimodal"]},
    )
    db_session.add(t)
    await db_session.flush()
    return t


class TestTenantService:
    """TenantService — 模块读写、套餐回退、基础模块保护。"""

    async def test_get_tenant_returns_first_active(
        self, db_session, tenant_free
    ):
        """无 tenant_id 时返回第一条活跃租户。"""
        service = TenantService(db_session)
        tenant = await service.get_tenant()
        assert tenant is not None
        assert tenant.name == "免费租户"

    async def test_get_tenant_by_id(self, db_session, tenant_pro):
        """按 ID 查询租户。"""
        service = TenantService(db_session)
        tenant = await service.get_tenant(tenant_pro.id)
        assert tenant is not None
        assert tenant.name == "专业租户"

    async def test_get_tenant_not_found(self, db_session):
        """不存在的 tenant_id 返回 None。"""
        service = TenantService(db_session)
        tenant = await service.get_tenant(uuid.uuid4())
        assert tenant is None

    async def test_get_enabled_modules_free_plan(self, db_session, tenant_free):
        """free 套餐默认仅含基础模块。"""
        service = TenantService(db_session)
        modules = await service.get_enabled_modules(tenant_free.id)
        assert set(modules) == BASIC_MODULE_IDS

    async def test_get_enabled_modules_pro_plan(self, db_session, tenant_pro):
        """pro 套餐包含基础 + 智能处理 + 部分集成模块。"""
        service = TenantService(db_session)
        modules = await service.get_enabled_modules(tenant_pro.id)
        assert "doc_intelligence" in modules
        assert "analytics_dashboard" in modules
        assert "knowledge_graph" in modules
        assert "expert_discovery" in modules
        assert "knowledge_push" in modules
        # pro 不含 unified_search 和 multimodal
        assert "unified_search" not in modules
        assert "multimodal" not in modules

    async def test_get_enabled_modules_with_settings(
        self, db_session, tenant_enterprise_with_settings
    ):
        """显式 settings 覆盖套餐默认。"""
        service = TenantService(db_session)
        modules = await service.get_enabled_modules(
            tenant_enterprise_with_settings.id
        )
        # settings 只配了 knowledge_base + multimodal，但基础模块自动补齐
        assert "knowledge_base" in modules
        assert "audit_workflow" in modules
        assert "qa_community" in modules
        assert "multimodal" in modules
        # 未在 settings 中的可选模块不启用
        assert "doc_intelligence" not in modules

    async def test_get_enabled_modules_no_tenant(self, db_session):
        """租户不存在时返回基础模块兜底。"""
        service = TenantService(db_session)
        modules = await service.get_enabled_modules()
        assert set(modules) == BASIC_MODULE_IDS

    async def test_is_module_enabled_basic(self, db_session, tenant_free):
        """基础模块永远启用（即使 free 套餐）。"""
        service = TenantService(db_session)
        assert await service.is_module_enabled("knowledge_base", tenant_free.id)
        assert await service.is_module_enabled("audit_workflow", tenant_free.id)
        assert await service.is_module_enabled("qa_community", tenant_free.id)

    async def test_is_module_enabled_optional_disabled(
        self, db_session, tenant_free
    ):
        """free 套餐不含可选模块。"""
        service = TenantService(db_session)
        assert not await service.is_module_enabled(
            "doc_intelligence", tenant_free.id
        )
        assert not await service.is_module_enabled("multimodal", tenant_free.id)

    async def test_is_module_enabled_optional_enabled(
        self, db_session, tenant_pro
    ):
        """pro 套餐包含 doc_intelligence。"""
        service = TenantService(db_session)
        assert await service.is_module_enabled(
            "doc_intelligence", tenant_pro.id
        )

    async def test_update_enabled_modules(self, db_session, tenant_free):
        """更新模块列表 — 自动补齐基础模块。"""
        service = TenantService(db_session)
        result = await service.update_enabled_modules(
            ["doc_intelligence", "multimodal"], tenant_free.id
        )
        # 基础模块自动包含
        assert "knowledge_base" in result
        assert "audit_workflow" in result
        assert "qa_community" in result
        # 用户指定的可选模块
        assert "doc_intelligence" in result
        assert "multimodal" in result
        # 验证持久化
        await db_session.refresh(tenant_free)
        assert "enabled_modules" in (tenant_free.settings or {})

    async def test_update_enabled_modules_invalid_id(
        self, db_session, tenant_free
    ):
        """无效模块 ID 抛出 ValueError。"""
        service = TenantService(db_session)
        with pytest.raises(ValueError, match="未知的模块 ID"):
            await service.update_enabled_modules(
                ["nonexistent"], tenant_free.id
            )

    async def test_update_enabled_modules_tenant_not_found(self, db_session):
        """租户不存在抛出 ValueError。"""
        service = TenantService(db_session)
        with pytest.raises(ValueError, match="租户不存在"):
            await service.update_enabled_modules(["doc_intelligence"])

    async def test_toggle_module_enable(self, db_session, tenant_free):
        """启用单个模块。"""
        service = TenantService(db_session)
        result = await service.toggle_module(
            "expert_discovery", True, tenant_free.id
        )
        assert "expert_discovery" in result

    async def test_toggle_module_disable(self, db_session, tenant_pro):
        """禁用单个可选模块。"""
        service = TenantService(db_session)
        result = await service.toggle_module(
            "doc_intelligence", False, tenant_pro.id
        )
        assert "doc_intelligence" not in result

    async def test_toggle_module_basic_cannot_disable(
        self, db_session, tenant_free
    ):
        """基础模块不可关闭 — 静默忽略。"""
        service = TenantService(db_session)
        result = await service.toggle_module(
            "knowledge_base", False, tenant_free.id
        )
        # knowledge_base 仍然在列表中
        assert "knowledge_base" in result

    async def test_toggle_module_invalid_id(self, db_session, tenant_free):
        """无效模块 ID 抛出 ValueError。"""
        service = TenantService(db_session)
        with pytest.raises(ValueError, match="未知的模块 ID"):
            await service.toggle_module("nonexistent", True, tenant_free.id)

    async def test_list_modules_with_status(self, db_session, tenant_free):
        """列出所有模块及启用状态。"""
        service = TenantService(db_session)
        modules = await service.list_modules_with_status(tenant_free.id)
        assert len(modules) == len(MODULE_REGISTRY)
        # 基础模块 enabled=True
        for m in modules:
            if m["is_basic"]:
                assert m["enabled"] is True
        # free 套餐可选模块 enabled=False
        doc_intel = next(
            m for m in modules if m["id"] == "doc_intelligence"
        )
        assert doc_intel["enabled"] is False

    async def test_list_modules_with_status_after_update(
        self, db_session, tenant_free
    ):
        """更新后模块状态正确反映。"""
        service = TenantService(db_session)
        await service.update_enabled_modules(
            ["doc_intelligence"], tenant_free.id
        )
        modules = await service.list_modules_with_status(tenant_free.id)
        doc_intel = next(
            m for m in modules if m["id"] == "doc_intelligence"
        )
        assert doc_intel["enabled"] is True
        multimodal = next(m for m in modules if m["id"] == "multimodal")
        assert multimodal["enabled"] is False


# ==================================================================
# 3. require_module 依赖测试
# ==================================================================


class TestRequireModule:
    """require_module — FastAPI 依赖注入门控。"""

    def _create_test_app(
        self, db_session: AsyncSession, tenant: Tenant | None
    ) -> tuple[TestClient, FastAPI]:
        """创建测试 FastAPI 应用。

        覆盖 get_db_session 和 get_current_user 依赖，
        使 require_module 能获取到模拟的数据库会话和用户。
        """
        app = FastAPI()

        # 模拟用户
        mock_user = User(
            email="test@test.com",
            hashed_password="fake",
            name="测试用户",
            role="admin",
            is_active=True,
        )
        mock_user.id = uuid.uuid4()

        # 如果有租户，预置到数据库
        if tenant is not None:
            db_session.add(tenant)
            # flush 确保 tenant 在 session 中

        async def override_db():
            yield db_session

        async def override_get_current_user():
            return mock_user

        from app.deps import get_current_user

        app.dependency_overrides[get_db_session] = override_db
        app.dependency_overrides[get_current_user] = override_get_current_user

        # 注册一个测试端点
        @app.get("/test-basic")
        async def test_basic(
            user: User = Depends(require_module("knowledge_base")),
        ):
            return {"ok": True, "module": "knowledge_base"}

        @app.get("/test-optional")
        async def test_optional(
            user: User = Depends(require_module("doc_intelligence")),
        ):
            return {"ok": True, "module": "doc_intelligence"}

        client = TestClient(app)
        return client, app

    async def test_basic_module_always_passes(self, db_session):
        """基础模块永远通过门控（即使无租户）。"""
        client, app = self._create_test_app(db_session, None)
        response = client.get("/test-basic")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    async def test_optional_module_blocked_without_tenant(
        self, db_session
    ):
        """无租户时可选模块被拦截（403）。"""
        client, app = self._create_test_app(db_session, None)
        response = client.get("/test-optional")
        assert response.status_code == 403
        assert "doc_intelligence" in response.json()["detail"]

    async def test_optional_module_blocked_free_plan(
        self, db_session, tenant_free
    ):
        """free 套餐不含 doc_intelligence → 403。"""
        client, app = self._create_test_app(db_session, tenant_free)
        response = client.get("/test-optional")
        assert response.status_code == 403

    async def test_optional_module_allowed_pro_plan(
        self, db_session, tenant_pro
    ):
        """pro 套餐包含 doc_intelligence → 200。"""
        client, app = self._create_test_app(db_session, tenant_pro)
        response = client.get("/test-optional")
        assert response.status_code == 200
        assert response.json()["module"] == "doc_intelligence"

    async def test_optional_module_allowed_with_settings(
        self, db_session, tenant_enterprise_with_settings
    ):
        """enterprise 租户 settings 中含 multimodal 但不含 doc_intelligence。

        doc_intelligence 应被拦截，但此处测试 require_module 的 settings 读取。
        """
        # tenant_enterprise_with_settings 的 settings 只配了 knowledge_base + multimodal
        # doc_intelligence 未在 settings 中
        client, app = self._create_test_app(
            db_session, tenant_enterprise_with_settings
        )
        response = client.get("/test-optional")
        # doc_intelligence 未在 settings 的 enabled_modules 中 → 403
        assert response.status_code == 403
