"""多租户隔离 P1 阶段测试 — 覆盖中间件、JWT、依赖注入、模型字段、Repository 过滤。

测试覆盖范围：
1. TestTenantContextMiddleware — 中间件从 JWT 解析 tenant_id 到 request.state
2. TestJWTTenantId — JWT payload 包含 tenant_id 的编解码
3. TestRequireModule — require_module 从 request.state.tenant_id 获取租户 ID
4. TestUserModelTenantId — User 模型 tenant_id 字段
5. TestQAModelTenantId — QaQuestion / QaAnswer 模型 tenant_id 字段
6. TestBaseRepositoryTenantFilter — BaseRepository 租户过滤与自动注入
7. TestGetTenantIdDependency — get_tenant_id 依赖从 request.state 获取租户 ID

测试策略：
- 中间件测试使用 fastapi.TestClient + setup_middleware（禁用限流）。
- JWT 测试直接调用 crypto 工具函数，不依赖外部服务。
- require_module 测试通过自定义中间件注入 request.state.tenant_id，
  验证门控逻辑正确读取租户上下文。
- 模型字段测试通过 __table__.columns 检查列定义。
- Repository 测试使用 PostgreSQL 数据库 + pytest fixture。
- get_tenant_id 测试使用 SimpleNamespace 模拟 Request 对象 + FastAPI 集成。
"""
from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ------------------------------------------------------------------
# Mock celery（仅当 celery 未安装时才 mock celery_app）
# ------------------------------------------------------------------
try:
    import celery  # noqa: F401
except ImportError:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.database import get_db_session
from app.deps import get_current_user, get_tenant_id, require_module
from app.models import Base, Department, QaAnswer, QaQuestion, Tenant, User
from app.repositories.base import BaseRepository
from app.utils.crypto import create_access_token, decode_access_token


# ==================================================================
# 辅助函数
# ==================================================================


def _create_middleware_test_app() -> FastAPI:
    """创建带租户上下文中间件的测试应用（禁用限流）。

    通过 mock settings 关闭限流，只保留 CORS + 租户上下文注入 + 日志中间件，
    使测试聚焦于 tenant_id 解析逻辑。
    """
    import app.middleware as mw
    from app.middleware import setup_middleware

    # 重置全局限流器，防止跨测试污染
    mw._rate_limiter = None

    settings = MagicMock()
    settings.CORS_ORIGINS = ["*"]
    settings.RATE_LIMIT_ENABLED = False
    settings.RATE_LIMIT_PER_MINUTE = 60
    settings.RATE_LIMIT_BURST = 2
    settings.REDIS_URL = None

    app = FastAPI()
    with patch("app.middleware.get_settings", return_value=settings):
        setup_middleware(app)

    @app.get("/api/test-tenant")
    async def test_tenant(request: Request):
        """返回 request.state.tenant_id 供测试断言。"""
        tid = getattr(request.state, "tenant_id", None)
        return {"tenant_id": str(tid) if tid else None}

    return app


# ==================================================================
# 数据库 fixture
# ==================================================================


@pytest_asyncio.fixture
async def db_session():
    """创建 PostgreSQL 数据库会话（自动建表）。

    每个测试独立创建引擎，测试结束后销毁，保证隔离。
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        # 先 drop 再 create — 清理前次测试残留数据，保证隔离
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def pro_tenant(db_session):
    """创建 pro 套餐租户（包含 doc_intelligence 等可选模块）。"""
    t = Tenant(name="Pro 租户", plan="pro", max_users=50, max_storage=1073741824)
    db_session.add(t)
    await db_session.flush()
    return t


# ==================================================================
# 1. TestTenantContextMiddleware
# ==================================================================


class TestTenantContextMiddleware:
    """测试中间件正确解析 tenant_id 到 request.state。

    覆盖场景：
    - JWT 中包含 tenant_id → request.state.tenant_id 正确设置
    - JWT 中无 tenant_id → request.state.tenant_id 为 None
    - 无 Authorization header → request.state.tenant_id 为 None
    - 无效 JWT → 不报错，request.state.tenant_id 为 None
    """

    def test_jwt_with_tenant_id_sets_state(self) -> None:
        """JWT 中包含 tenant_id 时，request.state.tenant_id 正确设置。"""
        app = _create_middleware_test_app()
        client = TestClient(app)

        tenant_id = uuid.uuid4()
        token = create_access_token({
            "sub": "user-1",
            "role": "admin",
            "tenant_id": str(tenant_id),
        })

        response = client.get(
            "/api/test-tenant",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(tenant_id)

    def test_jwt_without_tenant_id_sets_none(self) -> None:
        """JWT 中无 tenant_id 时，request.state.tenant_id 为 None。"""
        app = _create_middleware_test_app()
        client = TestClient(app)

        token = create_access_token({"sub": "user-1", "role": "admin"})

        response = client.get(
            "/api/test-tenant",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] is None

    def test_no_authorization_header_sets_none(self) -> None:
        """无 Authorization header 时，request.state.tenant_id 为 None。"""
        app = _create_middleware_test_app()
        client = TestClient(app)

        response = client.get("/api/test-tenant")
        assert response.status_code == 200
        assert response.json()["tenant_id"] is None

    def test_invalid_jwt_sets_none(self) -> None:
        """无效 JWT 时不报错，request.state.tenant_id 为 None。"""
        app = _create_middleware_test_app()
        client = TestClient(app)

        response = client.get(
            "/api/test-tenant",
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert response.status_code == 200  # 中间件不因无效 JWT 报错
        assert response.json()["tenant_id"] is None


# ==================================================================
# 2. TestJWTTenantId
# ==================================================================


class TestJWTTenantId:
    """测试 JWT payload 包含 tenant_id 的编解码。"""

    def test_create_token_with_tenant_id(self) -> None:
        """create_access_token 传入 tenant_id 时，decode_access_token 能解析出 tenant_id。"""
        tenant_id = uuid.uuid4()
        token = create_access_token({
            "sub": "user-1",
            "role": "admin",
            "tenant_id": str(tenant_id),
        })
        payload = decode_access_token(token)
        assert "tenant_id" in payload
        assert payload["tenant_id"] == str(tenant_id)

    def test_create_token_without_tenant_id(self) -> None:
        """create_access_token 不传 tenant_id 时，解析结果中 tenant_id 为 None。"""
        token = create_access_token({"sub": "user-1", "role": "admin"})
        payload = decode_access_token(token)
        assert payload.get("tenant_id") is None

    def test_tenant_id_roundtrip(self) -> None:
        """tenant_id 经 encode → decode 后值一致。"""
        tenant_id = uuid.uuid4()
        token = create_access_token({"tenant_id": str(tenant_id)})
        payload = decode_access_token(token)
        assert uuid.UUID(payload["tenant_id"]) == tenant_id


# ==================================================================
# 3. TestRequireModule
# ==================================================================


class TestRequireModule:
    """测试 require_module 从 request.state.tenant_id 获取租户 ID。

    覆盖场景：
    - require_module 内部从 request.state.tenant_id 获取租户 ID（pro 租户可选模块通过）
    - 无 tenant_id 时不报错（单租户兜底，基础模块通过）
    """

    def _create_test_app(
        self,
        db_session: AsyncSession,
        tenant_id: uuid.UUID | None = None,
    ) -> FastAPI:
        """创建测试应用，可选通过自定义中间件注入 request.state.tenant_id。

        通过 dependency_overrides 替换 get_db_session 和 get_current_user，
        使 require_module 能获取到模拟的数据库会话和用户。
        """
        app = FastAPI()

        # 通过自定义中间件注入 tenant_id（模拟 TenantContextMiddleware 的行为）
        if tenant_id is not None:
            tid = tenant_id

            @app.middleware("http")
            async def set_tenant_context(request: Request, call_next):
                request.state.tenant_id = tid
                return await call_next(request)

        # 模拟已认证用户
        mock_user = User(
            email="test@test.com",
            hashed_password="fake",
            name="测试用户",
            role="admin",
            is_active=True,
        )
        mock_user.id = uuid.uuid4()

        async def override_db():
            yield db_session

        async def override_get_current_user():
            return mock_user

        app.dependency_overrides[get_db_session] = override_db
        app.dependency_overrides[get_current_user] = override_get_current_user

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

        return app

    async def test_require_module_reads_tenant_from_state(
        self, db_session, pro_tenant
    ) -> None:
        """require_module 内部从 request.state.tenant_id 获取租户 ID。

        设置 request.state.tenant_id 为 pro 租户 ID，
        pro 套餐包含 doc_intelligence → 200 通过。
        """
        app = self._create_test_app(db_session, tenant_id=pro_tenant.id)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/test-optional")
        assert response.status_code == 200
        assert response.json()["module"] == "doc_intelligence"

    async def test_require_module_no_tenant_no_error(self, db_session) -> None:
        """无 tenant_id 时不报错（单租户兜底）— 基础模块通过。"""
        app = self._create_test_app(db_session, tenant_id=None)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/test-basic")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    async def test_require_module_optional_blocked_without_tenant(
        self, db_session
    ) -> None:
        """无 tenant_id 时可选模块被拦截（403），但不报 500 错误。"""
        app = self._create_test_app(db_session, tenant_id=None)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/test-optional")
        assert response.status_code == 403  # 门控拦截，非服务器错误


# ==================================================================
# 4. TestUserModelTenantId
# ==================================================================


class TestUserModelTenantId:
    """测试 User 模型 tenant_id 字段。"""

    def test_user_model_has_tenant_id(self) -> None:
        """User 模型有 tenant_id 字段。"""
        assert "tenant_id" in User.__table__.columns
        col = User.__table__.columns["tenant_id"]
        assert col.nullable is True, "tenant_id 应允许 NULL（私有部署兜底）"

    def test_can_create_user_with_tenant_id(self) -> None:
        """可以创建带 tenant_id 的 User 对象。"""
        tenant_id = uuid.uuid4()
        user = User(
            email="tenant-user@example.com",
            hashed_password="fake_hash",
            name="租户用户",
            role="admin",
            is_active=True,
            tenant_id=tenant_id,
        )
        assert user.tenant_id == tenant_id

    def test_can_create_user_without_tenant_id(self) -> None:
        """可以创建不带 tenant_id 的 User 对象（单租户兜底）。"""
        user = User(
            email="single-user@example.com",
            hashed_password="fake_hash",
            name="单租户用户",
            role="viewer",
            is_active=True,
        )
        assert user.tenant_id is None


# ==================================================================
# 5. TestQAModelTenantId
# ==================================================================


class TestQAModelTenantId:
    """测试 QA 模型 tenant_id 字段。"""

    def test_qa_question_has_tenant_id(self) -> None:
        """QaQuestion 模型有 tenant_id 字段。"""
        assert "tenant_id" in QaQuestion.__table__.columns
        col = QaQuestion.__table__.columns["tenant_id"]
        assert col.nullable is True, "QaQuestion.tenant_id 应允许 NULL"

    def test_qa_answer_has_tenant_id(self) -> None:
        """QaAnswer 模型有 tenant_id 字段。"""
        assert "tenant_id" in QaAnswer.__table__.columns
        col = QaAnswer.__table__.columns["tenant_id"]
        assert col.nullable is True, "QaAnswer.tenant_id 应允许 NULL"


# ==================================================================
# 6. TestBaseRepositoryTenantFilter
# ==================================================================


class TestBaseRepositoryTenantFilter:
    """测试 BaseRepository 租户过滤与自动注入。

    覆盖场景：
    - _apply_tenant_filter 在 tenant_id=None 时不过滤
    - _apply_tenant_filter 在 tenant_id 有值时追加 WHERE 条件
    - _apply_tenant_filter 对无 tenant_id 列的模型不过滤
    - create 方法自动写入 tenant_id
    - get_all 方法按租户过滤查询结果
    """

    def test_apply_tenant_filter_none_does_not_filter(self) -> None:
        """_apply_tenant_filter 在 tenant_id=None 时不过滤。"""
        mock_session = MagicMock()
        repo = BaseRepository(QaQuestion, mock_session, tenant_id=None)
        stmt = select(QaQuestion)
        filtered = repo._apply_tenant_filter(stmt)
        # tenant_id=None 时原样返回，无 WHERE 条件
        assert filtered.whereclause is None

    def test_apply_tenant_filter_with_tenant_adds_where(self) -> None:
        """_apply_tenant_filter 在 tenant_id 有值时追加 WHERE 条件。"""
        mock_session = MagicMock()
        tenant_id = uuid.uuid4()
        repo = BaseRepository(QaQuestion, mock_session, tenant_id=tenant_id)
        stmt = select(QaQuestion)
        filtered = repo._apply_tenant_filter(stmt)
        # 追加了 WHERE tenant_id = :tid 条件
        assert filtered.whereclause is not None

    def test_apply_tenant_filter_model_without_tenant_id(self) -> None:
        """_apply_tenant_filter 对无 tenant_id 列的模型不过滤。"""
        mock_session = MagicMock()
        tenant_id = uuid.uuid4()
        # Department 模型没有 tenant_id 列
        repo = BaseRepository(Department, mock_session, tenant_id=tenant_id)
        stmt = select(Department)
        filtered = repo._apply_tenant_filter(stmt)
        assert filtered.whereclause is None

    async def test_create_auto_injects_tenant_id(self, db_session) -> None:
        """create 方法自动写入 tenant_id。"""
        # 创建真实 User 和 Tenant 以满足外键约束
        user = User(email="fk-test1@test.com", hashed_password="x", name="U1", role="viewer")
        db_session.add(user)
        tenant = Tenant(name="测试租户", plan="free", max_users=10, max_storage=1024)
        db_session.add(tenant)
        await db_session.flush()

        tenant_id = tenant.id
        repo = BaseRepository(QaQuestion, db_session, tenant_id=tenant_id)

        question = await repo.create(
            user_id=user.id,
            title="测试问题",
            content="测试内容",
        )
        # tenant_id 由 repo 自动注入
        assert question.tenant_id == tenant_id

    async def test_create_does_not_override_explicit_tenant_id(
        self, db_session
    ) -> None:
        """create 方法不覆盖显式传入的 tenant_id。"""
        user = User(email="fk-test2@test.com", hashed_password="x", name="U2", role="viewer")
        db_session.add(user)
        tenant_repo = Tenant(name="租户A", plan="free", max_users=10, max_storage=1024)
        tenant_explicit = Tenant(name="租户B", plan="pro", max_users=50, max_storage=2048)
        db_session.add_all([tenant_repo, tenant_explicit])
        await db_session.flush()

        repo_tenant = tenant_repo.id
        explicit_tenant = tenant_explicit.id
        repo = BaseRepository(QaQuestion, db_session, tenant_id=repo_tenant)

        question = await repo.create(
            user_id=user.id,
            title="显式租户问题",
            content="测试内容",
            tenant_id=explicit_tenant,
        )
        # 显式传入的 tenant_id 优先
        assert question.tenant_id == explicit_tenant

    async def test_get_all_filters_by_tenant(self, db_session) -> None:
        """get_all 只返回当前租户的记录。"""
        # 创建真实 User 和 Tenant 以满足外键约束
        user = User(email="fk-getall@test.com", hashed_password="x", name="U", role="viewer")
        db_session.add(user)
        tenant_a = Tenant(name="租户A", plan="free", max_users=10, max_storage=1024)
        tenant_b = Tenant(name="租户B", plan="pro", max_users=50, max_storage=2048)
        db_session.add_all([tenant_a, tenant_b])
        await db_session.flush()

        tenant_a_id = tenant_a.id
        tenant_b_id = tenant_b.id

        # 直接创建记录（绕过 repo.create），设置不同的 tenant_id
        for i in range(3):
            db_session.add(QaQuestion(
                user_id=user.id,
                title=f"Tenant A - Q{i}",
                content="content",
                tenant_id=tenant_a_id,
            ))
        for i in range(2):
            db_session.add(QaQuestion(
                user_id=user.id,
                title=f"Tenant B - Q{i}",
                content="content",
                tenant_id=tenant_b_id,
            ))
        # 一条无 tenant_id 的记录（历史数据 / 单租户兜底）
        db_session.add(QaQuestion(
            user_id=user.id,
            title="No tenant",
            content="content",
            tenant_id=None,
        ))
        await db_session.flush()

        # Tenant A 的 repo 只能看到 A 的 3 条记录
        repo_a = BaseRepository(QaQuestion, db_session, tenant_id=tenant_a_id)
        results_a = await repo_a.get_all()
        assert len(results_a) == 3
        for r in results_a:
            assert r.tenant_id == tenant_a_id

        # Tenant B 的 repo 只能看到 B 的 2 条记录
        repo_b = BaseRepository(QaQuestion, db_session, tenant_id=tenant_b_id)
        results_b = await repo_b.get_all()
        assert len(results_b) == 2
        for r in results_b:
            assert r.tenant_id == tenant_b_id

        # 无 tenant_id 的 repo 能看到全部 6 条记录（单租户兜底）
        repo_none = BaseRepository(QaQuestion, db_session, tenant_id=None)
        results_none = await repo_none.get_all()
        assert len(results_none) == 6

    async def test_count_filters_by_tenant(self, db_session) -> None:
        """count 方法按租户过滤统计。"""
        user = User(email="fk-count@test.com", hashed_password="x", name="U", role="viewer")
        db_session.add(user)
        tenant_a = Tenant(name="计数A", plan="free", max_users=10, max_storage=1024)
        tenant_b = Tenant(name="计数B", plan="pro", max_users=50, max_storage=2048)
        db_session.add_all([tenant_a, tenant_b])
        await db_session.flush()

        tenant_a_id = tenant_a.id
        tenant_b_id = tenant_b.id

        for _ in range(4):
            db_session.add(QaQuestion(
                user_id=user.id,
                title="A",
                content="c",
                tenant_id=tenant_a_id,
            ))
        for _ in range(1):
            db_session.add(QaQuestion(
                user_id=user.id,
                title="B",
                content="c",
                tenant_id=tenant_b_id,
            ))
        await db_session.flush()

        repo_a = BaseRepository(QaQuestion, db_session, tenant_id=tenant_a_id)
        assert await repo_a.count() == 4

        repo_b = BaseRepository(QaQuestion, db_session, tenant_id=tenant_b_id)
        assert await repo_b.count() == 1

        repo_none = BaseRepository(QaQuestion, db_session, tenant_id=None)
        assert await repo_none.count() == 5


# ==================================================================
# 7. TestGetTenantIdDependency
# ==================================================================


class TestGetTenantIdDependency:
    """测试 get_tenant_id 依赖从 request.state 获取租户 ID。

    覆盖场景：
    - 从 request.state 获取 tenant_id
    - request.state 无 tenant_id 时返回 None
    - 作为 FastAPI 依赖在路由中使用
    """

    def test_get_tenant_id_from_state(self) -> None:
        """从 request.state 获取 tenant_id。"""
        tenant_id = uuid.uuid4()
        request = SimpleNamespace()
        request.state = SimpleNamespace(tenant_id=tenant_id)

        result = get_tenant_id(request)
        assert result == tenant_id

    def test_get_tenant_id_none_when_no_state(self) -> None:
        """request.state 无 tenant_id 时返回 None。"""
        request = SimpleNamespace()
        request.state = SimpleNamespace()  # 无 tenant_id 属性

        result = get_tenant_id(request)
        assert result is None

    def test_get_tenant_id_as_dependency(self) -> None:
        """作为 FastAPI 依赖使用时正确从 request.state 获取 tenant_id。"""
        tenant_id = uuid.uuid4()
        app = FastAPI()

        @app.middleware("http")
        async def set_tenant(request: Request, call_next):
            request.state.tenant_id = tenant_id
            return await call_next(request)

        @app.get("/test-tenant-id")
        async def test_endpoint(tid=Depends(get_tenant_id)):
            return {"tenant_id": str(tid) if tid else None}

        client = TestClient(app)
        response = client.get("/test-tenant-id")
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(tenant_id)

    def test_get_tenant_id_dependency_returns_none_without_middleware(
        self,
    ) -> None:
        """无中间件注入时 get_tenant_id 依赖返回 None。"""
        app = FastAPI()

        @app.get("/test-tenant-id")
        async def test_endpoint(tid=Depends(get_tenant_id)):
            return {"tenant_id": str(tid) if tid else None}

        client = TestClient(app)
        response = client.get("/test-tenant-id")
        assert response.status_code == 200
        assert response.json()["tenant_id"] is None
