"""多租户隔离 P3 阶段验证测试 — Service 层查询过滤全覆盖验证。

P3 修复了 Service 层原生 SQL 查询绕过租户过滤的问题。
本测试验证 Service 层的 list_kbs / list_questions / list_feedback 等方法
现在正确过滤租户数据。

测试链路：Service.list_xxx（原生 SQL + apply_tenant_filter）→ PostgreSQL DB
"""
from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.models import Base, KnowledgeBase, QaAnswer, QaQuestion, Tenant, User
from app.services.feedback_service import FeedbackService
from app.services.knowledge_service import KnowledgeService
from app.services.qa_service import QaService


# ==================================================================
# 数据库 fixture
# ==================================================================


@pytest_asyncio.fixture
async def db_session():
    """创建 PostgreSQL 数据库会话。"""
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
async def tenant_a(db_session):
    t = Tenant(name="租户 A", plan="pro", max_users=50, max_storage=1073741824)
    db_session.add(t)
    await db_session.flush()
    return t


@pytest_asyncio.fixture
async def tenant_b(db_session):
    t = Tenant(name="租户 B", plan="pro", max_users=50, max_storage=1073741824)
    db_session.add(t)
    await db_session.flush()
    return t


def _make_user(tenant_id=None, db_session=None) -> User:
    user = User(
        email=f"test-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="fake",
        name="测试用户",
        role="user",
        is_active=True,
        tenant_id=tenant_id,
    )
    user.id = uuid.uuid4()
    if db_session is not None:
        db_session.add(user)
    return user


# ==================================================================
# Service 层查询过滤验证
# ==================================================================


class TestServiceQueryTenantFilter:
    """验证 Service 层的原生 SQL 查询方法正确过滤租户。

    P3 修复前：list_kbs / list_questions / list_feedback 等方法绕过租户过滤。
    P3 修复后：这些方法通过 apply_tenant_filter 追加 WHERE tenant_id = :tid。
    """

    async def test_knowledge_service_list_kbs_isolated(
        self, db_session, tenant_a, tenant_b
    ) -> None:
        """KnowledgeService.list_kbs 现在正确过滤租户。"""
        user_a = _make_user(tenant_a.id, db_session)
        user_b = _make_user(tenant_b.id, db_session)
        await db_session.flush()

        # 租户 A 创建 2 个知识库
        svc_a = KnowledgeService(db_session, user_a, tenant_id=tenant_a.id)
        await svc_a.create_kb(name="A-KB1", description="A1")
        await svc_a.create_kb(name="A-KB2", description="A2")
        await db_session.commit()

        # 租户 B 创建 1 个知识库
        svc_b = KnowledgeService(db_session, user_b, tenant_id=tenant_b.id)
        await svc_b.create_kb(name="B-KB1", description="B1")
        await db_session.commit()

        # P3 修复后：list_kbs 应正确过滤租户
        result_a = await svc_a.list_kbs(page=1, size=20)
        assert result_a.total == 2, f"租户 A 应看到 2 个知识库，实际 {result_a.total}"

        result_b = await svc_b.list_kbs(page=1, size=20)
        assert result_b.total == 1, f"租户 B 应看到 1 个知识库，实际 {result_b.total}"

    async def test_qa_service_list_questions_isolated(
        self, db_session, tenant_a, tenant_b
    ) -> None:
        """QaService.list_questions 现在正确过滤租户。"""
        user_a = _make_user(tenant_a.id, db_session)
        await db_session.flush()
        svc_a = KnowledgeService(db_session, user_a, tenant_id=tenant_a.id)
        kb_a = await svc_a.create_kb(name="A-KB", description="A")
        await db_session.commit()

        user_b = _make_user(tenant_b.id, db_session)
        await db_session.flush()
        svc_b = KnowledgeService(db_session, user_b, tenant_id=tenant_b.id)
        kb_b = await svc_b.create_kb(name="B-KB", description="B")
        await db_session.commit()

        # 租户 A 创建 3 个问题
        qa_a = QaService(db_session, user_a, tenant_id=tenant_a.id)
        for i in range(3):
            await qa_a.create_question(
                kb_id=kb_a.id, title=f"A-Q{i}", content="content", tags="tag"
            )
        await db_session.commit()

        # 租户 B 创建 2 个问题
        qa_b = QaService(db_session, user_b, tenant_id=tenant_b.id)
        for i in range(2):
            await qa_b.create_question(
                kb_id=kb_b.id, title=f"B-Q{i}", content="content", tags="tag"
            )
        await db_session.commit()

        # P3 修复后：list_questions 应正确过滤租户
        result_a = await qa_a.list_questions(status=None, page=1, size=20)
        assert result_a.total == 3, f"租户 A 应看到 3 个问题，实际 {result_a.total}"

        result_b = await qa_b.list_questions(status=None, page=1, size=20)
        assert result_b.total == 2, f"租户 B 应看到 2 个问题，实际 {result_b.total}"

    async def test_feedback_service_list_isolated(
        self, db_session, tenant_a, tenant_b
    ) -> None:
        """FeedbackService.list_feedback 现在正确过滤租户。"""
        from app.models.feedback import Feedback

        user_a = _make_user(tenant_a.id, db_session)
        user_b = _make_user(tenant_b.id, db_session)
        await db_session.flush()

        # 直接创建反馈数据（绕过 Service 的 create 方法）
        for i in range(3):
            fb = Feedback(
                user_id=user_a.id,
                type="bug",
                content=f"A-反馈{i}",
                status="pending",
                tenant_id=tenant_a.id,
            )
            db_session.add(fb)
        for i in range(2):
            fb = Feedback(
                user_id=user_b.id,
                type="suggestion",
                content=f"B-反馈{i}",
                status="pending",
                tenant_id=tenant_b.id,
            )
            db_session.add(fb)
        await db_session.commit()

        # P3 修复后：list_feedback 应正确过滤租户
        svc_a = FeedbackService(db_session, user_a, tenant_id=tenant_a.id)
        result_a = await svc_a.list_feedback(page=1, size=20)
        assert result_a.total == 3, f"租户 A 应看到 3 条反馈，实际 {result_a.total}"

        svc_b = FeedbackService(db_session, user_b, tenant_id=tenant_b.id)
        result_b = await svc_b.list_feedback(page=1, size=20)
        assert result_b.total == 2, f"租户 B 应看到 2 条反馈，实际 {result_b.total}"

    async def test_tenant_none_list_all(self, db_session, tenant_a) -> None:
        """tenant_id=None 时 admin 用户能看到所有数据（单租户兜底）。"""
        user_a = _make_user(tenant_a.id, db_session)
        user_a.role = "admin"
        await db_session.flush()
        svc_a = KnowledgeService(db_session, user_a, tenant_id=tenant_a.id)
        await svc_a.create_kb(name="A-KB", description="A")
        await db_session.commit()

        user_none = _make_user(db_session=db_session)
        user_none.role = "admin"
        await db_session.flush()
        svc_none = KnowledgeService(db_session, user_none, tenant_id=None)
        result = await svc_none.list_kbs(page=1, size=20)
        assert result.total >= 1


# ==================================================================
# apply_tenant_filter 工具函数验证
# ==================================================================


class TestApplyTenantFilterUtil:
    """验证 app.utils.tenant.apply_tenant_filter 工具函数行为。"""

    def test_filter_applied_when_tenant_id_set(self) -> None:
        """tenant_id 不为 None 时追加 WHERE 条件。"""
        from sqlalchemy import select

        from app.models.knowledge import Document
        from app.utils.tenant import apply_tenant_filter

        tid = uuid.uuid4()
        stmt = select(Document).where(Document.deleted_at.is_(None))
        filtered = apply_tenant_filter(stmt, Document, tid)

        # 编译后的 SQL 应包含 tenant_id
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
        assert "tenant_id" in compiled.lower()

    def test_filter_skipped_when_tenant_id_none(self) -> None:
        """tenant_id 为 None 时不过滤。"""
        from sqlalchemy import select

        from app.models.knowledge import Document
        from app.utils.tenant import apply_tenant_filter

        stmt = select(Document).where(Document.deleted_at.is_(None))
        filtered = apply_tenant_filter(stmt, Document, None)

        # 语句应与原始语句相同
        compiled_original = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        compiled_filtered = str(
            filtered.compile(compile_kwargs={"literal_binds": True})
        )
        assert compiled_original == compiled_filtered

    def test_filter_skipped_when_model_has_no_tenant_id(self) -> None:
        """模型没有 tenant_id 列时不过滤。"""
        from sqlalchemy import select

        from app.models import Tenant
        from app.utils.tenant import apply_tenant_filter

        tid = uuid.uuid4()
        stmt = select(Tenant)
        filtered = apply_tenant_filter(stmt, Tenant, tid)

        # Tenant 模型没有 tenant_id 列，语句不应被修改
        compiled_original = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        compiled_filtered = str(
            filtered.compile(compile_kwargs={"literal_binds": True})
        )
        assert compiled_original == compiled_filtered

    def test_filter_works_with_update_statement(self) -> None:
        """apply_tenant_filter 支持 UPDATE 语句。"""
        from sqlalchemy import update

        from app.models.knowledge import Document
        from app.utils.tenant import apply_tenant_filter

        tid = uuid.uuid4()
        stmt = update(Document).where(Document.id == uuid.uuid4())
        filtered = apply_tenant_filter(stmt, Document, tid)

        compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
        assert "tenant_id" in compiled.lower()
