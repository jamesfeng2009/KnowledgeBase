"""文档审核流程串联测试 — 验证 document_tasks → AuditService → 发布的完整链路。

覆盖：
- _submit_for_audit：正确创建 AuditFlow 记录
- _publish_document：正确更新文档状态为 published
- _process_document_async：按密级路由（confidential/secret → 审核，public/internal → 直接发布）
- AuditService.approve：审核通过后触发 _publish_document
- AuditService.reject：驳回不触发发布
- 密级路由边界条件
"""
from __future__ import annotations

import sys
import uuid as _uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

if "opensearchpy" not in sys.modules:
    sys.modules["opensearchpy"] = MagicMock()

if "pymilvus" not in sys.modules:
    sys.modules["pymilvus"] = MagicMock()

_TEST_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


# ======================================================================
# _submit_for_audit 测试
# ======================================================================


class TestSubmitForAudit:
    """_submit_for_audit() 函数测试 — 提交文档审核。"""

    @pytest.mark.asyncio
    async def test_creates_audit_flow_record(self) -> None:
        """提交审核时创建 AuditFlow 记录。"""
        from tasks.document_tasks import _submit_for_audit

        mock_repo = MagicMock()
        mock_repo.create = AsyncMock()

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        owner_id = _uuid.UUID(_TEST_UUID)

        with patch("app.database.task_db_session", return_value=mock_session_cm), \
             patch("app.repositories.base.BaseRepository", return_value=mock_repo):

            await _submit_for_audit(_TEST_UUID, owner_id)

        mock_repo.create.assert_called_once()
        call_kwargs = mock_repo.create.call_args.kwargs
        assert call_kwargs["resource_type"] == "document"
        assert call_kwargs["resource_id"] == _uuid.UUID(_TEST_UUID)
        assert call_kwargs["submitter_id"] == owner_id
        assert call_kwargs["priority"] == "normal"

    @pytest.mark.asyncio
    async def test_commit_called_after_create(self) -> None:
        """创建 AuditFlow 后提交事务。"""
        from tasks.document_tasks import _submit_for_audit

        mock_repo = MagicMock()
        mock_repo.create = AsyncMock()

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.task_db_session", return_value=mock_session_cm), \
             patch("app.repositories.base.BaseRepository", return_value=mock_repo):

            await _submit_for_audit(_TEST_UUID, _uuid.UUID(_TEST_UUID))

        mock_session.commit.assert_called_once()


# ======================================================================
# _publish_document 测试
# ======================================================================


class TestPublishDocument:
    """_publish_document() 函数测试 — 审核通过后发布文档。"""

    @pytest.mark.asyncio
    async def test_updates_status_to_published(self) -> None:
        """发布文档时将状态从 pending_review 更新为 published。"""
        from tasks.document_tasks import _publish_document

        mock_doc = MagicMock()
        mock_doc.status = "pending_review"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.task_db_session", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo):

            await _publish_document(_TEST_UUID)

        assert mock_doc.status == "published"
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_nonexistent_doc_logs_warning(self) -> None:
        """发布不存在的文档时记录警告，不抛异常。"""
        from tasks.document_tasks import _publish_document

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.task_db_session", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo):

            # 不应抛出异常
            await _publish_document(_TEST_UUID)

        mock_session.commit.assert_not_called()


# ======================================================================
# _process_document_async 密级路由测试
# ======================================================================


class TestDocumentClassificationRouting:
    """_process_document_async() 密级路由测试 — 按密级决定是否审核。"""

    def _make_mock_doc(self, classification: str = "internal") -> MagicMock:
        """创建模拟 Document 对象。"""
        mock_doc = MagicMock()
        mock_doc.id = _uuid.UUID(_TEST_UUID)
        mock_doc.content_text = "# 标题\n\n这是文档内容。" * 20
        mock_doc.content_html = None
        mock_doc.doc_type = "md"
        mock_doc.status = "draft"
        mock_doc.file_path = None
        mock_doc.classification = classification
        mock_doc.owner_id = _uuid.UUID(_TEST_UUID)
        return mock_doc

    def _setup_mocks(self, mock_doc: MagicMock) -> dict[str, Any]:
        """设置通用 Mock，返回各 patch 上下文管理器。"""
        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        return {
            "repo": mock_repo,
            "session": mock_session,
            "session_cm": mock_session_cm,
        }

    @pytest.mark.asyncio
    async def test_confidential_doc_goes_to_review(self) -> None:
        """confidential 密级文档进入待审核状态。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc(classification="confidential")
        mocks = self._setup_mocks(mock_doc)

        with patch("app.database.task_db_session", return_value=mocks["session_cm"]), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mocks["repo"]), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["status"] == "success"
        assert result["doc_status"] == "pending_review"
        mock_audit.assert_called_once()
        assert mock_doc.status == "pending_review"

    @pytest.mark.asyncio
    async def test_secret_doc_goes_to_review(self) -> None:
        """secret 密级文档进入待审核状态。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc(classification="secret")
        mocks = self._setup_mocks(mock_doc)

        with patch("app.database.task_db_session", return_value=mocks["session_cm"]), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mocks["repo"]), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["doc_status"] == "pending_review"
        mock_audit.assert_called_once()

    @pytest.mark.asyncio
    async def test_public_doc_published_directly(self) -> None:
        """public 密级文档直接发布，不进入审核。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc(classification="public")
        mocks = self._setup_mocks(mock_doc)

        with patch("app.database.task_db_session", return_value=mocks["session_cm"]), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mocks["repo"]), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["doc_status"] == "published"
        mock_audit.assert_not_called()
        assert mock_doc.status == "published"

    @pytest.mark.asyncio
    async def test_internal_doc_published_directly(self) -> None:
        """internal 密级文档直接发布，不进入审核。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc(classification="internal")
        mocks = self._setup_mocks(mock_doc)

        with patch("app.database.task_db_session", return_value=mocks["session_cm"]), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mocks["repo"]), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["doc_status"] == "published"
        mock_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_classification_defaults_to_internal(self) -> None:
        """classification 为 None 时默认 internal，直接发布。"""
        from tasks.document_tasks import _process_document_async

        mock_doc = self._make_mock_doc(classification=None)
        mocks = self._setup_mocks(mock_doc)

        with patch("app.database.task_db_session", return_value=mocks["session_cm"]), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mocks["repo"]), \
             patch("tasks.document_tasks._generate_embeddings", new_callable=AsyncMock, return_value=[]), \
             patch("tasks.document_tasks._build_indexes", new_callable=AsyncMock), \
             patch("tasks.document_tasks._submit_for_audit", new_callable=AsyncMock) as mock_audit, \
             patch("tasks.intelligence_tasks.process_intelligence.delay"):

            result = await _process_document_async(_TEST_UUID)

        assert result["doc_status"] == "published"
        mock_audit.assert_not_called()


# ======================================================================
# AuditService.approve 文档发布触发测试
# ======================================================================


class TestAuditServiceApprovePublish:
    """AuditService.approve() 审核通过后触发文档发布测试。"""

    @pytest.mark.asyncio
    async def test_approve_document_triggers_publish(self) -> None:
        """审核通过 document 类型时触发 _publish_document。"""
        from app.services.audit_service import AuditService

        audit_id = _uuid.UUID(_TEST_UUID)
        resource_id = _uuid.UUID(_TEST_UUID)

        mock_audit = MagicMock()
        mock_audit.id = audit_id
        mock_audit.status = "pending"
        mock_audit.resource_type = "document"
        mock_audit.resource_id = resource_id

        mock_updated_audit = MagicMock()
        mock_updated_audit.id = audit_id
        mock_updated_audit.status = "approved"
        mock_updated_audit.resource_type = "document"
        mock_updated_audit.resource_id = resource_id

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_audit)
        mock_repo.update = AsyncMock(return_value=mock_updated_audit)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        mock_db = MagicMock()

        service = AuditService(mock_db, mock_user)
        service._repo = mock_repo

        with patch(
            "tasks.document_tasks._publish_document",
            new_callable=AsyncMock,
        ) as mock_publish:
            result = await service.approve(audit_id, comment="内容合规")

        mock_repo.update.assert_called_once()
        mock_publish.assert_called_once_with(str(resource_id))
        assert result.status == "approved"

    @pytest.mark.asyncio
    async def test_approve_non_document_does_not_trigger_publish(self) -> None:
        """审核通过非 document 类型时不触发 _publish_document。"""
        from app.services.audit_service import AuditService

        audit_id = _uuid.UUID(_TEST_UUID)
        resource_id = _uuid.UUID(_TEST_UUID)

        mock_audit = MagicMock()
        mock_audit.id = audit_id
        mock_audit.status = "pending"
        mock_audit.resource_type = "kb"
        mock_audit.resource_id = resource_id

        mock_updated_audit = MagicMock()
        mock_updated_audit.status = "approved"
        mock_updated_audit.resource_type = "kb"
        mock_updated_audit.resource_id = resource_id

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_audit)
        mock_repo.update = AsyncMock(return_value=mock_updated_audit)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        service = AuditService(MagicMock(), mock_user)
        service._repo = mock_repo

        with patch(
            "tasks.document_tasks._publish_document",
            new_callable=AsyncMock,
        ) as mock_publish:
            await service.approve(audit_id)

        mock_publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_approve_publish_failure_does_not_affect_approval(self) -> None:
        """文档发布失败不影响审核通过状态。"""
        from app.services.audit_service import AuditService

        audit_id = _uuid.UUID(_TEST_UUID)

        mock_audit = MagicMock()
        mock_audit.id = audit_id
        mock_audit.status = "pending"
        mock_audit.resource_type = "document"
        mock_audit.resource_id = _uuid.UUID(_TEST_UUID)

        mock_updated_audit = MagicMock()
        mock_updated_audit.status = "approved"
        mock_updated_audit.resource_type = "document"
        mock_updated_audit.resource_id = _uuid.UUID(_TEST_UUID)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_audit)
        mock_repo.update = AsyncMock(return_value=mock_updated_audit)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        service = AuditService(MagicMock(), mock_user)
        service._repo = mock_repo

        with patch(
            "tasks.document_tasks._publish_document",
            new_callable=AsyncMock,
            side_effect=Exception("DB connection lost"),
        ):
            # 不应抛出异常 — 审核已通过
            result = await service.approve(audit_id)

        assert result.status == "approved"

    @pytest.mark.asyncio
    async def test_approve_nonexistent_audit_raises(self) -> None:
        """审核不存在的流程抛出 ValueError。"""
        from app.services.audit_service import AuditService

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=None)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        service = AuditService(MagicMock(), mock_user)
        service._repo = mock_repo

        with pytest.raises(ValueError, match="审核流程不存在"):
            await service.approve(_uuid.UUID(_TEST_UUID))

    @pytest.mark.asyncio
    async def test_approve_already_processed_raises(self) -> None:
        """审核已处理的流程抛出 ValueError。"""
        from app.services.audit_service import AuditService

        mock_audit = MagicMock()
        mock_audit.status = "approved"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_audit)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        service = AuditService(MagicMock(), mock_user)
        service._repo = mock_repo

        with pytest.raises(ValueError, match="审核流程已处理"):
            await service.approve(_uuid.UUID(_TEST_UUID))


# ======================================================================
# AuditService.reject 驳回：不发布 + 文档复位草稿
# ======================================================================


class TestAuditServiceReject:
    """AuditService.reject() 驳回 — 不触发发布，document 类型复位为草稿。"""

    @pytest.mark.asyncio
    async def test_reject_document_reverts_to_draft(self) -> None:
        """驳回 document 类型审核时触发 _revert_document_to_draft。"""
        from app.services.audit_service import AuditService

        mock_audit = MagicMock()
        mock_audit.status = "pending"
        mock_audit.resource_type = "document"
        mock_audit.resource_id = _uuid.UUID(_TEST_UUID)

        mock_updated = MagicMock()
        mock_updated.status = "rejected"
        mock_updated.resource_type = "document"
        mock_updated.resource_id = _uuid.UUID(_TEST_UUID)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_audit)
        mock_repo.update = AsyncMock(return_value=mock_updated)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        service = AuditService(MagicMock(), mock_user)
        service._repo = mock_repo

        with patch(
            "app.services.audit_service.AuditService._revert_document_after_reject",
            new_callable=AsyncMock,
        ) as mock_revert, patch(
            "tasks.document_tasks._publish_document",
            new_callable=AsyncMock,
        ) as mock_publish:
            result = await service.reject(_uuid.UUID(_TEST_UUID), comment="内容不合规")

        mock_publish.assert_not_called()
        mock_revert.assert_called_once_with(str(_TEST_UUID))
        assert result.status == "rejected"

    @pytest.mark.asyncio
    async def test_reject_non_document_does_not_revert(self) -> None:
        """驳回非 document 类型审核时不触发文档复位。"""
        from app.services.audit_service import AuditService

        mock_audit = MagicMock()
        mock_audit.status = "pending"
        mock_audit.resource_type = "kb"
        mock_audit.resource_id = _uuid.UUID(_TEST_UUID)

        mock_updated = MagicMock()
        mock_updated.status = "rejected"
        mock_updated.resource_type = "kb"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_audit)
        mock_repo.update = AsyncMock(return_value=mock_updated)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        service = AuditService(MagicMock(), mock_user)
        service._repo = mock_repo

        with patch(
            "app.services.audit_service.AuditService._revert_document_after_reject",
            new_callable=AsyncMock,
        ) as mock_revert:
            await service.reject(_uuid.UUID(_TEST_UUID))

        mock_revert.assert_not_called()

    @pytest.mark.asyncio
    async def test_reject_revert_failure_does_not_affect_rejection(self) -> None:
        """驳回后文档复位失败不影响驳回状态。"""
        from app.services.audit_service import AuditService

        mock_audit = MagicMock()
        mock_audit.status = "pending"
        mock_audit.resource_type = "document"
        mock_audit.resource_id = _uuid.UUID(_TEST_UUID)

        mock_updated = MagicMock()
        mock_updated.status = "rejected"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_audit)
        mock_repo.update = AsyncMock(return_value=mock_updated)

        mock_user = MagicMock()
        mock_user.id = _uuid.UUID(_TEST_UUID)

        service = AuditService(MagicMock(), mock_user)
        service._repo = mock_repo

        with patch(
            "app.services.audit_service.AuditService._revert_document_after_reject",
            new_callable=AsyncMock,
            side_effect=Exception("DB connection lost"),
        ):
            # 不应抛出异常 — 驳回已生效
            result = await service.reject(_uuid.UUID(_TEST_UUID))

        assert result.status == "rejected"


# ======================================================================
# _revert_document_to_draft 测试
# ======================================================================


class TestRevertDocumentToDraft:
    """_revert_document_to_draft() 函数测试 — 驳回后文档复位为草稿。"""

    @pytest.mark.asyncio
    async def test_reverts_status_to_draft(self) -> None:
        """驳回后文档状态从 pending_review 复位为 draft 并提交。"""
        from tasks.document_tasks import _revert_document_to_draft

        mock_doc = MagicMock()
        mock_doc.status = "pending_review"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.task_db_session", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._refresh_index_doc_status", new_callable=AsyncMock) as mock_refresh:

            await _revert_document_to_draft(_TEST_UUID)

        assert mock_doc.status == "draft"
        mock_session.commit.assert_called_once()
        mock_refresh.assert_awaited_once()
        call_kwargs = mock_refresh.call_args
        assert call_kwargs.args[0] == _TEST_UUID
        assert call_kwargs.kwargs["doc_status"] == "draft"

    @pytest.mark.asyncio
    async def test_revert_nonexistent_doc_logs_warning(self) -> None:
        """驳回不存在的文档时记录警告，不抛异常、不提交。"""
        from tasks.document_tasks import _revert_document_to_draft

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.task_db_session", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._refresh_index_doc_status", new_callable=AsyncMock) as mock_refresh:

            await _revert_document_to_draft(_TEST_UUID)

        mock_session.commit.assert_not_called()
        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_revert_index_refresh_failure_is_swallowed(self) -> None:
        """复位成功后索引刷新失败仅记录告警，不影响复位结果。"""
        from tasks.document_tasks import _revert_document_to_draft

        mock_doc = MagicMock()
        mock_doc.status = "pending_review"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=mock_doc)

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.database.task_db_session", return_value=mock_session_cm), \
             patch("app.repositories.knowledge_repository.DocumentRepository", return_value=mock_repo), \
             patch("tasks.document_tasks._refresh_index_doc_status", new_callable=AsyncMock,
                   side_effect=Exception("OS unavailable")):
            await _revert_document_to_draft(_TEST_UUID)

        assert mock_doc.status == "draft"
        mock_session.commit.assert_called_once()


# ======================================================================
# _REQUIRES_REVIEW 常量测试
# ======================================================================


class TestRequiresReviewConstant:
    """_REQUIRES_REVIEW 常量测试 — 验证密级路由配置。"""

    def test_confidential_requires_review(self) -> None:
        """confidential 密级需要审核。"""
        from tasks.document_tasks import _REQUIRES_REVIEW
        assert "confidential" in _REQUIRES_REVIEW

    def test_secret_requires_review(self) -> None:
        """secret 密级需要审核。"""
        from tasks.document_tasks import _REQUIRES_REVIEW
        assert "secret" in _REQUIRES_REVIEW

    def test_public_does_not_require_review(self) -> None:
        """public 密级不需要审核。"""
        from tasks.document_tasks import _REQUIRES_REVIEW
        assert "public" not in _REQUIRES_REVIEW

    def test_internal_does_not_require_review(self) -> None:
        """internal 密级不需要审核。"""
        from tasks.document_tasks import _REQUIRES_REVIEW
        assert "internal" not in _REQUIRES_REVIEW
