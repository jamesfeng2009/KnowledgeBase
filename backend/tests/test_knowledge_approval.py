"""知识回流审批服务单元测试 — P2 审批工作流。

测试覆盖：
- TestKnowledgeApprovalService: 审批服务核心逻辑
    - submit_for_review: 自动通过分流（高质量/低质量/有冲突/有 PII）
    - approve: 人工批准 + 状态流转
    - reject: 人工拒绝 + 文档软删除
    - 状态护栏：非 pending 不可审批
- TestPiiDetection: PII 检测
- TestSubmitFaqForReview: compounding_service 审批接入 + 降级
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ------------------------------------------------------------------
# Mock celery before importing app modules
# ------------------------------------------------------------------
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


def _make_mock_db():
    """创建 Mock AsyncSession。"""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.scalar = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _make_mock_user(role: str = "admin"):
    """创建 Mock User。"""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.role = role
    return user


def _make_mock_asset(
    confidence: float = 0.95,
    title: str = "公司差旅报销标准是什么？",
    content: str = "经济舱按实报销，商务舱需审批。",
):
    """创建 Mock KnowledgeAsset。"""
    asset = MagicMock()
    asset.id = uuid.uuid4()
    asset.doc_id = uuid.uuid4()
    asset.title = title
    asset.content = content
    asset.confidence_score = confidence
    asset.status = "pending_review"
    return asset


def _make_scalar_one_or_none_result(value):
    """创建 mock DB execute 结果 — scalar_one_or_none() 返回指定值。"""
    mock = MagicMock()
    mock.scalar_one_or_none = MagicMock(return_value=value)
    return mock


# ======================================================================
# KnowledgeApprovalService 单元测试
# ======================================================================


class TestKnowledgeApprovalService:
    """P2 知识回流审批服务测试。"""

    @pytest.mark.asyncio
    async def test_submit_for_review_auto_approve_high_quality(self):
        """高质量资产自动通过（quality_score >= 0.9 且无冲突且无 PII）。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        asset = _make_mock_asset(confidence=0.95)

        with patch.object(
            service, "_detect_pii", return_value=(False, [])
        ), patch.object(
            service, "_update_doc_status", new=AsyncMock()
        ):
            approval = await service.submit_for_review(
                asset=asset,
                doc_id=asset.doc_id,
                kb_id=uuid.uuid4(),
                conflict_count=0,
            )

        assert approval.status == "approved"
        assert approval.auto_approved is True
        assert asset.status == "active"

    @pytest.mark.asyncio
    async def test_submit_for_review_pending_low_quality(self):
        """低质量资产进入人工审批（quality_score < 0.9）。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        asset = _make_mock_asset(confidence=0.5)

        with patch.object(
            service, "_detect_pii", return_value=(False, [])
        ), patch.object(
            service, "_update_doc_status", new=AsyncMock()
        ):
            approval = await service.submit_for_review(
                asset=asset,
                doc_id=asset.doc_id,
                kb_id=uuid.uuid4(),
                conflict_count=0,
            )

        assert approval.status == "pending"
        assert approval.auto_approved is False
        assert asset.status == "pending_review"
        assert approval.expire_at is not None

    @pytest.mark.asyncio
    async def test_submit_for_review_pending_with_conflicts(self):
        """有冲突的资产进入人工审批（即使质量高）。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        asset = _make_mock_asset(confidence=0.95)

        with patch.object(
            service, "_detect_pii", return_value=(False, [])
        ), patch.object(
            service, "_update_doc_status", new=AsyncMock()
        ):
            approval = await service.submit_for_review(
                asset=asset,
                doc_id=asset.doc_id,
                kb_id=uuid.uuid4(),
                conflict_count=2,  # 有冲突
            )

        assert approval.status == "pending"
        assert approval.auto_approved is False
        assert approval.conflict_count == 2

    @pytest.mark.asyncio
    async def test_submit_for_review_pending_with_pii(self):
        """含 PII 的资产进入人工审批（即使质量高）。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        asset = _make_mock_asset(confidence=0.95)

        with patch.object(
            service, "_detect_pii", return_value=(True, ["phone"])
        ), patch.object(
            service, "_update_doc_status", new=AsyncMock()
        ):
            approval = await service.submit_for_review(
                asset=asset,
                doc_id=asset.doc_id,
                kb_id=uuid.uuid4(),
                conflict_count=0,
            )

        assert approval.status == "pending"
        assert approval.auto_approved is False
        assert approval.pii_detected is True

    @pytest.mark.asyncio
    async def test_approve_success(self):
        """人工批准 — asset.status=active, doc.status=published。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)

        approval = MagicMock()
        approval.id = uuid.uuid4()
        approval.asset_id = uuid.uuid4()
        approval.doc_id = uuid.uuid4()
        approval.status = "pending"

        asset = _make_mock_asset()

        user = _make_mock_user()

        with patch.object(
            service, "_get_approval", new=AsyncMock(return_value=approval)
        ), patch.object(
            service, "_get_asset", new=AsyncMock(return_value=asset)
        ), patch.object(
            service, "_update_doc_status", new=AsyncMock()
        ):
            result = await service.approve(approval.id, user, note="通过")

        assert result.status == "approved"
        assert result.reviewer_id == user.id
        assert result.review_note == "通过"
        assert asset.status == "active"

    @pytest.mark.asyncio
    async def test_reject_success(self):
        """人工拒绝 — asset.status=deprecated, doc 软删除。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)

        approval = MagicMock()
        approval.id = uuid.uuid4()
        approval.asset_id = uuid.uuid4()
        approval.doc_id = uuid.uuid4()
        approval.status = "pending"

        asset = _make_mock_asset()
        user = _make_mock_user()

        with patch.object(
            service, "_get_approval", new=AsyncMock(return_value=approval)
        ), patch.object(
            service, "_get_asset", new=AsyncMock(return_value=asset)
        ), patch.object(
            service, "_soft_delete_doc", new=AsyncMock()
        ):
            result = await service.reject(approval.id, user, reason="内容不准确")

        assert result.status == "rejected"
        assert result.reviewer_id == user.id
        assert result.review_note == "内容不准确"
        assert asset.status == "deprecated"

    @pytest.mark.asyncio
    async def test_approve_non_pending_raises(self):
        """状态护栏：非 pending 状态不可批准。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)

        approval = MagicMock()
        approval.id = uuid.uuid4()
        approval.status = "approved"  # 已批准

        user = _make_mock_user()

        with patch.object(
            service, "_get_approval", new=AsyncMock(return_value=approval)
        ):
            with pytest.raises(ValueError, match="审批状态非 pending"):
                await service.approve(approval.id, user)

    @pytest.mark.asyncio
    async def test_approve_not_found_raises(self):
        """审批不存在时报错。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        user = _make_mock_user()

        with patch.object(
            service, "_get_approval", new=AsyncMock(return_value=None)
        ):
            with pytest.raises(ValueError, match="审批不存在"):
                await service.approve(uuid.uuid4(), user)

    @pytest.mark.asyncio
    async def test_reject_non_pending_raises(self):
        """状态护栏：非 pending 状态不可拒绝。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)

        approval = MagicMock()
        approval.id = uuid.uuid4()
        approval.status = "rejected"  # 已拒绝

        user = _make_mock_user()

        with patch.object(
            service, "_get_approval", new=AsyncMock(return_value=approval)
        ):
            with pytest.raises(ValueError, match="审批状态非 pending"):
                await service.reject(approval.id, user, reason="test")


# ======================================================================
# PII 检测测试
# ======================================================================


class TestPiiDetection:
    """PII 检测测试 — 复用 PIIScrubber。"""

    def test_detect_pii_with_phone(self):
        """含手机号 → pii_detected=True。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        detected, risks = service._detect_pii("联系我:13800138000")
        assert detected is True
        assert "phone" in risks

    def test_detect_pii_with_email(self):
        """含邮箱 → pii_detected=True。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        detected, risks = service._detect_pii("发到 test@example.com")
        assert detected is True
        assert "email" in risks

    def test_detect_pii_clean_text(self):
        """无 PII 文本 → pii_detected=False。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        detected, risks = service._detect_pii("公司差旅报销标准是什么？")
        assert detected is False
        assert risks == []

    def test_detect_pii_empty_text(self):
        """空文本 → pii_detected=False。"""
        from app.services.knowledge_approval_service import (
            KnowledgeApprovalService,
        )

        db = _make_mock_db()
        service = KnowledgeApprovalService(db)
        detected, risks = service._detect_pii("", "")
        assert detected is False


# ======================================================================
# compounding_service 审批接入测试
# ======================================================================


class TestSubmitFaqForReview:
    """P2 compounding_service._submit_faq_for_review 接入测试。"""

    @pytest.mark.asyncio
    async def test_submit_faq_for_review_degraded_on_failure(self):
        """审批服务失败时优雅降级（不阻断沉淀）。"""
        from app.services.knowledge_compounding import (
            KnowledgeCompoundingService,
        )

        db = _make_mock_db()
        llm = MagicMock()
        service = KnowledgeCompoundingService(llm, db)

        asset = _make_mock_asset()

        # patch KnowledgeApprovalService.submit_for_review 抛异常
        with patch(
            "app.services.knowledge_approval_service.KnowledgeApprovalService.submit_for_review",
            new=AsyncMock(side_effect=RuntimeError("approval service down")),
        ):
            # 不应抛异常（降级）
            await service._submit_faq_for_review(
                asset=asset,
                target_kb_id=uuid.uuid4(),
                conflict_count=0,
            )

        # 资产保持 pending_review（降级未改 status）
        assert asset.status == "pending_review"

    @pytest.mark.asyncio
    async def test_submit_faq_for_review_calls_service(self):
        """正常调用 KnowledgeApprovalService.submit_for_review。"""
        from app.services.knowledge_compounding import (
            KnowledgeCompoundingService,
        )

        db = _make_mock_db()
        llm = MagicMock()
        service = KnowledgeCompoundingService(llm, db)

        asset = _make_mock_asset()

        with patch(
            "app.services.knowledge_approval_service.KnowledgeApprovalService.submit_for_review",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_submit:
            await service._submit_faq_for_review(
                asset=asset,
                target_kb_id=uuid.uuid4(),
                conflict_count=1,
            )

        mock_submit.assert_called_once()
        # 验证传入 conflict_count
        call_kwargs = mock_submit.call_args.kwargs
        assert call_kwargs["conflict_count"] == 1
        assert call_kwargs["asset"] == asset
