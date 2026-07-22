"""AuditFlow 租户隔离验证测试 — MT-1~MT-7 多租户补列。

测试覆盖：
1. AuditFlow 模型 tenant_id 列存在性
2. AuditRepository 查询方法应用 tenant_id 过滤
3. AuditService.list_pending() 应用 tenant_id 过滤
4. AuditService.submit_for_review() 创建时设置 tenant_id
5. 迁移文件包含 audit_flows RLS 策略
6. 参数化配置项存在且验证正确
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
# 1. AuditFlow 模型 tenant_id 列验证
# ==================================================================


class TestAuditFlowModel:
    """验证 AuditFlow 模型已正确添加 tenant_id 列。"""

    def test_audit_flow_has_tenant_id_column(self):
        """AuditFlow 模型应包含 tenant_id 列定义。"""
        from app.models.audit import AuditFlow

        assert hasattr(AuditFlow, "tenant_id"), "AuditFlow 模型应包含 tenant_id 属性"
        col = AuditFlow.__table__.columns.get("tenant_id")
        assert col is not None, "audit_flows 表应包含 tenant_id 列"
        assert col.nullable is True, "tenant_id 列应为 nullable（兼容私有部署）"

    def test_audit_flow_has_tenant_id_index(self):
        """audit_flows 表应有 tenant_id 索引。"""
        from app.models.audit import AuditFlow

        index_names = [idx.name for idx in AuditFlow.__table__.indexes]
        assert "ix_audit_flows_tenant_id" in index_names, (
            "audit_flows 表应包含 ix_audit_flows_tenant_id 索引"
        )


# ==================================================================
# 2. AuditRepository 租户过滤验证
# ==================================================================


class TestAuditRepositoryTenantFilter:
    """验证 AuditRepository 的查询方法已应用租户过滤。"""

    def test_repository_docstring_no_warning(self):
        """AuditRepository 类文档不应再包含'无法过滤'警告。"""
        from app.repositories.audit_repository import AuditRepository

        docstring = AuditRepository.__doc__ or ""
        assert "无法过滤" not in docstring, (
            "AuditRepository 文档不应再包含'无法过滤'警告"
        )
        assert "tenant_id" in docstring, (
            "AuditRepository 文档应说明 tenant_id 过滤已激活"
        )

    def test_get_by_status_uses_apply_all_filters(self):
        """get_by_status 方法应调用 _apply_all_filters 注入租户过滤。"""
        from app.repositories.audit_repository import AuditRepository

        # 检查方法源码是否包含 _apply_all_filters 调用
        import inspect

        source = inspect.getsource(AuditRepository.get_by_status)
        assert "_apply_all_filters" in source, (
            "get_by_status 应通过 _apply_all_filters 注入租户过滤"
        )

    def test_get_by_resource_uses_apply_all_filters(self):
        """get_by_resource 方法应调用 _apply_all_filters。"""
        from app.repositories.audit_repository import AuditRepository

        import inspect

        source = inspect.getsource(AuditRepository.get_by_resource)
        assert "_apply_all_filters" in source

    def test_get_by_submitter_uses_apply_all_filters(self):
        """get_by_submitter 方法应调用 _apply_all_filters。"""
        from app.repositories.audit_repository import AuditRepository

        import inspect

        source = inspect.getsource(AuditRepository.get_by_submitter)
        assert "_apply_all_filters" in source

    def test_get_by_reviewer_uses_apply_all_filters(self):
        """get_by_reviewer 方法应调用 _apply_all_filters。"""
        from app.repositories.audit_repository import AuditRepository

        import inspect

        source = inspect.getsource(AuditRepository.get_by_reviewer)
        assert "_apply_all_filters" in source


# ==================================================================
# 3. AuditService 租户过滤验证
# ==================================================================


class TestAuditServiceTenantFilter:
    """验证 AuditService 的查询方法已应用租户过滤。"""

    def test_list_pending_filters_tenant_id(self):
        """list_pending 方法应包含 tenant_id 过滤条件。"""
        from app.services.audit_service import AuditService

        import inspect

        source = inspect.getsource(AuditService.list_pending)
        assert "tenant_id" in source, (
            "list_pending 方法应包含 tenant_id 过滤逻辑"
        )

    def test_submit_for_review_sets_tenant_id(self):
        """submit_for_review 方法应设置 tenant_id。"""
        from app.services.audit_service import AuditService

        import inspect

        source = inspect.getsource(AuditService.submit_for_review)
        assert "tenant_id" in source, (
            "submit_for_review 方法应设置 tenant_id"
        )


# ==================================================================
# 4. 迁移文件验证
# ==================================================================


class TestAuditFlowMigration:
    """验证 audit_flows 迁移文件的结构和内容。"""

    @pytest.fixture
    def migration_path(self) -> Path:
        """返回 audit_flows 迁移文件路径。"""
        base = Path(__file__).resolve().parent.parent / "alembic" / "versions"
        files = list(base.glob("*add_tenant_id_audit_flows*"))
        assert len(files) >= 1, "应存在 audit_flows tenant_id 迁移文件"
        return files[0]

    def test_migration_adds_tenant_id_column(self, migration_path: Path):
        """迁移文件应包含添加 tenant_id 列的语句。"""
        content = migration_path.read_text()
        assert "tenant_id" in content
        assert "audit_flows" in content

    def test_migration_adds_rls_policy(self, migration_path: Path):
        """迁移文件应为 audit_flows 表创建 RLS 策略。"""
        content = migration_path.read_text()
        assert "ENABLE ROW LEVEL SECURITY" in content
        assert "FORCE ROW LEVEL SECURITY" in content
        assert "CREATE POLICY" in content
        assert "audit_flows" in content

    def test_migration_adds_file_size_to_documents(self, migration_path: Path):
        """迁移文件应同时修复 BUG-3（documents 加 file_size）。"""
        content = migration_path.read_text()
        assert "file_size" in content
        assert "documents" in content


# ==================================================================
# 5. 参数化配置验证
# ==================================================================


class TestParameterizedConfig:
    """验证 P1-A/P1-B 参数化配置项存在且验证正确。"""

    def test_retry_config_exists(self):
        """退避重试参数应存在于 Settings 中。"""
        from app.config import Settings

        s = Settings()
        assert s.RETRY_BACKOFF_BASE > 0
        assert s.RETRY_BACKOFF_MAX > 0
        assert s.RETRY_MAX_ATTEMPTS > 0
        assert s.RETRY_JITTER >= 0
        assert s.RETRY_BACKOFF_BASE_CELERY > 0
        assert s.RETRY_BACKOFF_BASE_DB > 0

    def test_circuit_breaker_config_exists(self):
        """熔断器参数应存在于 Settings 中。"""
        from app.config import Settings

        s = Settings()
        assert s.CIRCUIT_BREAKER_FAILURE_THRESHOLD > 0
        assert s.CIRCUIT_BREAKER_RECOVERY_TIMEOUT > 0
        assert s.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS > 0

    def test_task_lock_config_exists(self):
        """幂等锁参数应存在于 Settings 中。"""
        from app.config import Settings

        s = Settings()
        assert s.TASK_LOCK_TTL > 0
        assert s.TASK_LOCK_REDIS_PREFIX

    def test_shutdown_config_exists(self):
        """优雅关闭参数应存在于 Settings 中。"""
        from app.config import Settings

        s = Settings()
        assert s.SHUTDOWN_TIMEOUT > 0
        assert s.SHUTDOWN_GRACE_PERIOD_CORE > 0
        assert s.SHUTDOWN_GRACE_PERIOD_CELERY > 0

    def test_positive_float_validator_rejects_zero(self):
        """正浮点数验证器应拒绝 0 值。"""
        from app.config import Settings
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(RETRY_BACKOFF_BASE=0.0)

    def test_positive_int_validator_rejects_zero(self):
        """正整数验证器应拒绝 0 值。"""
        from app.config import Settings
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(CIRCUIT_BREAKER_FAILURE_THRESHOLD=0)


# ==================================================================
# 6. Document 模型 file_size 字段验证（BUG-3）
# ==================================================================


class TestDocumentFileSize:
    """验证 BUG-3 修复：Document 模型已添加 file_size 字段。"""

    def test_document_has_file_size_column(self):
        """Document 模型应包含 file_size 列。"""
        from app.models.knowledge import Document

        col = Document.__table__.columns.get("file_size")
        assert col is not None, "documents 表应包含 file_size 列（BUG-3 修复）"
        assert col.nullable is True


# ==================================================================
# 7. video_tasks.py Bug 修复验证
# ==================================================================


class TestVideoTasksBugFix:
    """验证 BUG-1 和 BUG-2 修复。"""

    def test_video_tasks_imports_celery_app(self):
        """video_tasks.py 应导入 celery_app（BUG-2 修复）。"""
        import tasks.video_tasks as vt

        assert hasattr(vt, "celery_app"), "video_tasks 应导入 celery_app"

    def test_process_video_multipart_is_task(self):
        """process_video_multipart 应注册为 Celery 任务（BUG-2 修复）。"""
        import tasks.video_tasks as vt

        assert hasattr(vt.process_video_multipart, "delay"), (
            "process_video_multipart 应有 .delay 方法（注册为 Celery 任务）"
        )

    def test_asr_multipart_task_is_task(self):
        """asr_multipart_task 应注册为 Celery 任务。"""
        import tasks.video_tasks as vt

        assert hasattr(vt.asr_multipart_task, "delay")

    def test_keyframe_task_is_task(self):
        """keyframe_task 应注册为 Celery 任务。"""
        import tasks.video_tasks as vt

        assert hasattr(vt.keyframe_task, "delay")

    def test_finalize_video_task_is_task(self):
        """finalize_video_task 应注册为 Celery 任务。"""
        import tasks.video_tasks as vt

        assert hasattr(vt.finalize_video_task, "delay")

    def test_no_process_document_intelligence_reference(self):
        """video_tasks.py 不应引用不存在的 process_document_intelligence（BUG-1 修复）。"""
        import tasks.video_tasks as vt

        source = open(vt.__file__).read()
        assert "process_document_intelligence" not in source, (
            "video_tasks.py 不应引用不存在的 process_document_intelligence"
        )
        assert "process_intelligence" in source, (
            "video_tasks.py 应引用正确的 process_intelligence"
        )


# ==================================================================
# 8. celery_app.py include 验证（BUG-4）
# ==================================================================


class TestCeleryIncludeFix:
    """验证 BUG-4 修复：celery_app.py include 列表完整。

    注意：由于测试环境 mock 了 celery_app，直接导入无法获取真实 include 列表，
    改为读取源文件内容验证。
    """

    @pytest.fixture
    def celery_app_source(self) -> str:
        """读取 celery_app.py 源文件内容。"""
        path = Path(__file__).resolve().parent.parent / "celery_app.py"
        return path.read_text()

    def test_include_contains_intelligence_tasks(self, celery_app_source: str):
        """include 列表应包含 intelligence_tasks。"""
        assert "tasks.intelligence_tasks" in celery_app_source

    def test_include_contains_compounding_tasks(self, celery_app_source: str):
        """include 列表应包含 compounding_tasks。"""
        assert "tasks.compounding_tasks" in celery_app_source

    def test_include_contains_testing_tasks(self, celery_app_source: str):
        """include 列表应包含 testing_tasks。"""
        assert "tasks.testing_tasks" in celery_app_source


# ==================================================================
# 9. index_tasks.py BUG-5 修复验证
# ==================================================================


class TestRebuildIndexBugFix:
    """验证 BUG-5 修复：rebuild_kb_index 先删除旧索引。"""

    def test_delete_kb_indices_function_exists(self):
        """_delete_kb_indices 函数应存在。"""
        import tasks.index_tasks as it

        assert hasattr(it, "_delete_kb_indices"), (
            "index_tasks 应包含 _delete_kb_indices 函数（BUG-5 修复）"
        )

    def test_rebuild_calls_delete_before_build(self):
        """_rebuild_kb_index_async 应在构建前调用 _delete_kb_indices。"""
        import inspect

        from tasks.index_tasks import _rebuild_kb_index_async

        source = inspect.getsource(_rebuild_kb_index_async)
        delete_pos = source.find("_delete_kb_indices")
        build_pos = source.find("success_count = 0")
        assert delete_pos > 0, "应在重建前调用 _delete_kb_indices"
        assert build_pos > 0
        assert delete_pos < build_pos, "删除旧索引应在构建新索引之前"
