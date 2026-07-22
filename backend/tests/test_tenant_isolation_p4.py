"""多租户隔离 P4 阶段验证测试 — RLS 策略迁移 + 结构化日志上下文绑定。

测试覆盖：
1. TestRLSMigration — RLS 迁移文件结构和内容验证
2. TestStructlogContextBinding — structlog contextvars 绑定 tenant_id 验证
3. TestMiddlewareContextCleanup — 中间件清理 structlog 上下文验证
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ==================================================================
# 1. TestRLSMigration — RLS 迁移文件验证
# ==================================================================


class TestRLSMigration:
    """验证 RLS 迁移文件的结构和内容。

    RLS（Row-Level Security）是 L4 防御层，在 PostgreSQL 层面强制租户隔离。
    迁移文件应：
    - 对所有 29 个 tenant-scoped 表启用 RLS
    - 创建 tenant_isolation 策略
    - 使用 current_setting('app.tenant_id', true) 读取会话变量
    """

    @pytest.fixture
    def rls_migration_content(self) -> str:
        """读取 RLS 迁移文件内容。"""
        backend = Path(__file__).resolve().parent.parent
        versions_dir = backend / "alembic" / "versions"
        rls_files = list(versions_dir.glob("*rls*.py"))
        assert len(rls_files) >= 1, "应有 RLS 迁移文件"
        return rls_files[0].read_text()

    def test_rls_migration_file_exists(self) -> None:
        """RLS 迁移文件存在。"""
        backend = Path(__file__).resolve().parent.parent
        versions_dir = backend / "alembic" / "versions"
        rls_files = list(versions_dir.glob("*rls*.py"))
        assert len(rls_files) >= 1, "应有 RLS 迁移文件"

    def test_rls_migration_revision_chain(self, rls_migration_content: str) -> None:
        """RLS 迁移的 down_revision 指向 tenant_id 迁移。"""
        assert 'revision: str = "b8c9d0e1f2a3"' in rls_migration_content
        assert 'down_revision: str | None = "a7b8c9d0e1f2"' in rls_migration_content

    def test_rls_migration_has_upgrade_and_downgrade(
        self, rls_migration_content: str
    ) -> None:
        """RLS 迁移包含 upgrade() 和 downgrade() 函数。"""
        assert "def upgrade()" in rls_migration_content
        assert "def downgrade()" in rls_migration_content

    def test_rls_migration_enables_rls(self, rls_migration_content: str) -> None:
        """RLS 迁移包含 ENABLE ROW LEVEL SECURITY 语句。"""
        assert "ENABLE ROW LEVEL SECURITY" in rls_migration_content

    def test_rls_migration_forces_rls(self, rls_migration_content: str) -> None:
        """RLS 迁移包含 FORCE ROW LEVEL SECURITY 语句。"""
        assert "FORCE ROW LEVEL SECURITY" in rls_migration_content

    def test_rls_migration_creates_policy(self, rls_migration_content: str) -> None:
        """RLS 迁移创建 tenant_isolation 策略。"""
        assert "CREATE POLICY tenant_isolation" in rls_migration_content
        assert "FOR ALL" in rls_migration_content

    def test_rls_migration_uses_session_variable(
        self, rls_migration_content: str
    ) -> None:
        """RLS 策略使用 current_setting('app.tenant_id', true) 读取会话变量。"""
        assert "current_setting('app.tenant_id', true)" in rls_migration_content

    def test_rls_migration_has_using_and_with_check(
        self, rls_migration_content: str
    ) -> None:
        """RLS 策略包含 USING 和 WITH CHECK 子句。"""
        assert "USING" in rls_migration_content
        assert "WITH CHECK" in rls_migration_content

    def test_rls_migration_covers_all_tenant_tables(
        self, rls_migration_content: str
    ) -> None:
        """RLS 迁移覆盖所有 tenant-scoped 表。"""
        expected_tables = [
            "users",
            "qa_questions",
            "qa_answers",
            "knowledge_bases",
            "documents",
            "conversations",
            "messages",
            "feedbacks",
            "document_comments",
            "notifications",
            "knowledge_assets",
            "compounding_tasks",
            "knowledge_conflicts",
            "test_projects",
            "test_cases",
            "test_plans",
            "api_keys",
            "subscriptions",
            "usage_records",
        ]
        for table in expected_tables:
            assert f'"{table}"' in rls_migration_content, (
                f"RLS 迁移应覆盖表 {table}"
            )

    def test_rls_migration_downgrade_drops_policy(
        self, rls_migration_content: str
    ) -> None:
        """downgrade 删除策略并禁用 RLS。"""
        assert "DROP POLICY" in rls_migration_content
        assert "DISABLE ROW LEVEL SECURITY" in rls_migration_content
        assert "NO FORCE ROW LEVEL SECURITY" in rls_migration_content


# ==================================================================
# 2. TestStructlogContextBinding — structlog 上下文绑定验证
# ==================================================================


class TestStructlogContextBinding:
    """验证 structlog contextvars 正确绑定 tenant_id。

    中间件应使用 structlog.contextvars.bind_contextvars 绑定 tenant_id，
    使当前请求内所有日志条目自动携带 tenant_id。
    请求结束后应调用 clear_contextvars 清理上下文。
    """

    def test_middleware_imports_structlog(self) -> None:
        """中间件导入了 structlog。"""
        middleware_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "middleware.py"
        )
        content = middleware_path.read_text()
        assert "import structlog" in content

    def test_middleware_binds_tenant_id(self) -> None:
        """中间件使用 bind_contextvars 绑定 tenant_id。"""
        middleware_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "middleware.py"
        )
        content = middleware_path.read_text()
        assert "structlog.contextvars.bind_contextvars" in content
        assert "tenant_id" in content

    def test_middleware_clears_context(self) -> None:
        """中间件在 finally 块中清理 structlog 上下文。"""
        middleware_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "middleware.py"
        )
        content = middleware_path.read_text()
        assert "structlog.contextvars.clear_contextvars" in content
        assert "finally" in content

    def test_logger_has_merge_contextvars_processor(self) -> None:
        """logger 配置包含 merge_contextvars 处理器。"""
        logger_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "utils"
            / "logger.py"
        )
        content = logger_path.read_text()
        assert "merge_contextvars" in content

    def test_structlog_context_bind_and_clear(self) -> None:
        """structlog contextvars 的 bind/clear 机制正常工作。"""
        import structlog

        # 清理可能存在的上下文
        structlog.contextvars.clear_contextvars()

        # 绑定 tenant_id
        test_tid = str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(tenant_id=test_tid)

        # 验证上下文变量已设置
        ctx = structlog.contextvars.get_contextvars()
        assert ctx.get("tenant_id") == test_tid

        # 清理
        structlog.contextvars.clear_contextvars()
        ctx_after = structlog.contextvars.get_contextvars()
        assert "tenant_id" not in ctx_after

    def test_structlog_context_none_tenant(self) -> None:
        """tenant_id=None 时 structlog 上下文正确绑定 None。"""
        import structlog

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(tenant_id=None)

        ctx = structlog.contextvars.get_contextvars()
        assert ctx.get("tenant_id") is None

        structlog.contextvars.clear_contextvars()


# ==================================================================
# 3. TestDatabaseSessionRLS — get_db_session RLS 预备验证
# ==================================================================


class TestDatabaseSessionRLS:
    """验证 get_db_session 正确设置 app.tenant_id 会话变量。

    get_db_session 应在 PostgreSQL 上执行 SET LOCAL app.tenant_id = :tid，
    为 RLS 策略提供会话变量。
    """

    def test_database_py_imports_text(self) -> None:
        """database.py 导入了 sqlalchemy text。"""
        db_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "database.py"
        )
        content = db_path.read_text()
        assert "from sqlalchemy import text" in content or "from sqlalchemy.text" in content

    def test_database_py_sets_tenant_id(self) -> None:
        """get_db_session 执行 SET LOCAL app.tenant_id。"""
        db_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "database.py"
        )
        content = db_path.read_text()
        assert "SET LOCAL app.tenant_id" in content

    def test_database_py_reads_request_state(self) -> None:
        """get_db_session 从 request.state 读取 tenant_id。"""
        db_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "database.py"
        )
        content = db_path.read_text()
        assert "request.state.tenant_id" in content or "getattr(request.state" in content

    def test_database_py_imports_request(self) -> None:
        """database.py 导入了 Request。"""
        db_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "database.py"
        )
        content = db_path.read_text()
        assert "from fastapi import Request" in content or "Request" in content


# ==================================================================
# 4. TestTenantUtilFunction — apply_tenant_filter 工具函数
# ==================================================================


class TestTenantUtilFunction:
    """验证 app.utils.tenant.apply_tenant_filter 工具函数。"""

    def test_tenant_util_file_exists(self) -> None:
        """tenant.py 工具文件存在。"""
        util_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "utils"
            / "tenant.py"
        )
        assert util_path.exists(), "app/utils/tenant.py 应存在"

    def test_tenant_util_has_apply_function(self) -> None:
        """tenant.py 导出了 apply_tenant_filter 函数。"""
        from app.utils.tenant import apply_tenant_filter

        assert callable(apply_tenant_filter)

    def test_tenant_util_function_signature(self) -> None:
        """apply_tenant_filter 接受 stmt, model, tenant_id 三个参数。"""
        import inspect

        from app.utils.tenant import apply_tenant_filter

        sig = inspect.signature(apply_tenant_filter)
        params = list(sig.parameters.keys())
        assert "stmt" in params
        assert "model" in params
        assert "tenant_id" in params

    def test_tenant_util_none_tenant_returns_original(self) -> None:
        """tenant_id=None 时返回原始语句。"""
        from sqlalchemy import select

        from app.models.knowledge import Document
        from app.utils.tenant import apply_tenant_filter

        stmt = select(Document)
        result = apply_tenant_filter(stmt, Document, None)
        assert result is stmt

    def test_tenant_util_model_without_tenant_id(self) -> None:
        """模型没有 tenant_id 列时返回原始语句。"""
        from sqlalchemy import select

        from app.models import Tenant
        from app.utils.tenant import apply_tenant_filter

        stmt = select(Tenant)
        result = apply_tenant_filter(stmt, Tenant, uuid.uuid4())
        assert result is stmt
