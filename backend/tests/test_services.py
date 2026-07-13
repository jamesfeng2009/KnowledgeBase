"""业务服务测试 — 权限过滤、质量评分、缺口检测、反馈闭环。

验证点：
- PermissionService.filter_documents 按密级正确过滤（admin 路径，无需 DB）；
- QualityService._score_completeness 字段完整度评分；
- GapDetectorService.record_no_result 空查询跳过 + 递增计数；
- FeedbackLoopService.process_feedback 状态流转 + 自动回复。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.feedback_loop_service import FeedbackLoopService
from app.services.gap_detector_service import GapDetectorService
from app.services.permission_service import _CLEARANCE_ORDER
from app.services.quality_service import (
    WEIGHT_COMPLETENESS,
    WEIGHT_FEEDBACK,
    WEIGHT_CITATION,
    QualityService,
)


# ======================================================================
# 权限服务测试
# ======================================================================


class TestPermissionService:
    """PermissionService 文档过滤测试 — admin 路径（纯内存，无 DB）。"""

    @pytest.mark.asyncio
    async def test_permission_service_filter_admin_internal(self) -> None:
        """admin + internal 密级：仅保留 public/internal，过滤 confidential/secret。"""
        from app.services.permission_service import PermissionService

        user = SimpleNamespace(role="admin", clearance_level="internal", id=uuid4())
        service = PermissionService(db=AsyncMock(), user=user)

        docs = [
            SimpleNamespace(classification="public", kb_id=uuid4()),
            SimpleNamespace(classification="internal", kb_id=uuid4()),
            SimpleNamespace(classification="confidential", kb_id=uuid4()),
            SimpleNamespace(classification="secret", kb_id=uuid4()),
        ]

        result = await service.filter_documents(docs)

        classifications = {d.classification for d in result}
        assert classifications == {"public", "internal"}
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_permission_service_filter_admin_secret(self) -> None:
        """admin + secret 密级：可访问全部密级。"""
        from app.services.permission_service import PermissionService

        user = SimpleNamespace(role="admin", clearance_level="secret", id=uuid4())
        service = PermissionService(db=AsyncMock(), user=user)

        docs = [
            SimpleNamespace(classification="public", kb_id=uuid4()),
            SimpleNamespace(classification="secret", kb_id=uuid4()),
        ]

        result = await service.filter_documents(docs)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_permission_service_filter_unknown_classification(self) -> None:
        """未知 classification 默认为 internal 级别（权重 1）。"""
        from app.services.permission_service import PermissionService

        user = SimpleNamespace(role="admin", clearance_level="internal", id=uuid4())
        service = PermissionService(db=AsyncMock(), user=user)

        docs = [SimpleNamespace(classification="weird_level", kb_id=uuid4())]

        result = await service.filter_documents(docs)

        # _CLEARANCE_ORDER.get("weird_level", 1) = 1 <= 1 (internal) -> 保留
        assert len(result) == 1

    def test_allowed_classifications(self) -> None:
        """allowed_classifications 返回的密级应与 filter_documents 一致。"""
        from app.services.permission_service import PermissionService

        user = SimpleNamespace(role="admin", clearance_level="internal", id=uuid4())
        service = PermissionService(db=AsyncMock(), user=user)

        allowed = service.allowed_classifications()

        assert "public" in allowed
        assert "internal" in allowed
        assert "confidential" not in allowed
        assert "secret" not in allowed


# ======================================================================
# 质量服务测试
# ======================================================================


class TestQualityService:
    """QualityService 完整度评分测试 — 纯方法，无需 DB。"""

    def test_quality_service_score_full(self) -> None:
        """所有字段齐全 -> 完整度 1.0。"""
        service = QualityService(db=AsyncMock())
        doc = SimpleNamespace(
            title="测试文档",
            content_text="正文内容",
            content_html="<p>正文</p>",
            classification="internal",
            file_path="/docs/test.md",
        )

        score = service._score_completeness(doc)

        assert score == pytest.approx(1.0)

    def test_quality_service_score_partial(self) -> None:
        """缺少 2 个字段 -> 完整度 0.6。"""
        service = QualityService(db=AsyncMock())
        doc = SimpleNamespace(
            title="测试文档",
            content_text="正文内容",
            content_html=None,
            classification="internal",
            file_path=None,
        )

        score = service._score_completeness(doc)

        assert score == pytest.approx(0.6)

    def test_quality_service_score_empty(self) -> None:
        """所有字段为空 -> 完整度 0.0。"""
        service = QualityService(db=AsyncMock())
        doc = SimpleNamespace(
            title="",
            content_text="",
            content_html="",
            classification="",
            file_path="",
        )

        score = service._score_completeness(doc)

        assert score == pytest.approx(0.0)

    def test_quality_weights_sum_to_one(self) -> None:
        """三个维度权重之和应为 1.0。"""
        assert WEIGHT_COMPLETENESS + WEIGHT_CITATION + WEIGHT_FEEDBACK == pytest.approx(1.0)


# ======================================================================
# 缺口检测服务测试
# ======================================================================


class TestGapDetectorService:
    """GapDetectorService 记录无结果查询测试。"""

    @pytest.mark.asyncio
    async def test_gap_detector_record(self) -> None:
        """有效查询应调用 gap_repo.increment_search_count。"""
        db = AsyncMock()
        service = GapDetectorService(db)
        service.gap_repo = AsyncMock()
        service.gap_repo.increment_search_count.return_value = SimpleNamespace(
            id=uuid4(), topic="报销流程", search_count=1, priority="low"
        )

        await service.record_no_result("报销流程")

        service.gap_repo.increment_search_count.assert_called_once_with("报销流程")

    @pytest.mark.asyncio
    async def test_gap_detector_record_strips_whitespace(self) -> None:
        """查询应被 strip 后传给 repo。"""
        db = AsyncMock()
        service = GapDetectorService(db)
        service.gap_repo = AsyncMock()
        service.gap_repo.increment_search_count.return_value = SimpleNamespace(
            id=uuid4(), topic="报销流程", search_count=1, priority="low"
        )

        await service.record_no_result("  报销流程  ")

        service.gap_repo.increment_search_count.assert_called_once_with("报销流程")

    @pytest.mark.asyncio
    async def test_gap_detector_record_empty_skipped(self) -> None:
        """空查询 / 纯空白查询应被跳过，不调用 repo。"""
        db = AsyncMock()
        service = GapDetectorService(db)
        service.gap_repo = AsyncMock()

        await service.record_no_result("")
        await service.record_no_result("   ")
        await service.record_no_result(None)  # type: ignore[arg-type]

        service.gap_repo.increment_search_count.assert_not_called()


# ======================================================================
# 反馈闭环服务测试
# ======================================================================


class TestFeedbackLoopService:
    """FeedbackLoopService 反馈处理测试。"""

    def test_generate_auto_response_by_type(self) -> None:
        """_generate_auto_response 应根据反馈类型返回对应回复。"""
        service = FeedbackLoopService(db=AsyncMock())

        cases = [
            ("bug", "缺陷"),
            ("suggestion", "建议"),
            ("praise", "肯定"),
            ("complaint", "投诉"),
        ]
        for fb_type, keyword in cases:
            fb = SimpleNamespace(type=fb_type)
            resp = service._generate_auto_response(fb)
            assert keyword in resp, f"类型 {fb_type} 的回复应包含 '{keyword}'"

    def test_generate_auto_response_unknown_type(self) -> None:
        """未知反馈类型应返回通用回复。"""
        service = FeedbackLoopService(db=AsyncMock())
        fb = SimpleNamespace(type="unknown")

        resp = service._generate_auto_response(fb)

        assert "处理中" in resp

    @pytest.mark.asyncio
    async def test_feedback_loop_process(self) -> None:
        """process_feedback 应按类型流转状态并生成自动回复。"""
        fb_id = uuid4()
        feedback = SimpleNamespace(
            id=fb_id, type="bug", status="open", response=None,
            updated_at=datetime.now(),
        )
        updated = SimpleNamespace(
            id=fb_id, type="bug", status="processing",
            response="已收到缺陷反馈，正在调查中，我们会尽快修复。",
            updated_at=datetime.now(),
        )

        service = FeedbackLoopService(db=AsyncMock())
        service.feedback_repo = AsyncMock()
        service.feedback_repo.get_by_id.return_value = feedback
        service.feedback_repo.update.return_value = updated

        result = await service.process_feedback(fb_id)

        assert result["status"] == "processing"
        assert "缺陷" in result["response"]
        service.feedback_repo.update.assert_called_once()
        # 验证 update 传入的 status 参数
        call_kwargs = service.feedback_repo.update.call_args
        assert call_kwargs.kwargs.get("status") == "processing"

    @pytest.mark.asyncio
    async def test_feedback_loop_process_praise_resolved(self) -> None:
        """praise 类型应直接 resolved（无需处理）。"""
        fb_id = uuid4()
        feedback = SimpleNamespace(
            id=fb_id, type="praise", status="open", response=None,
            updated_at=datetime.now(),
        )
        updated = SimpleNamespace(
            id=fb_id, type="praise", status="resolved",
            response="感谢您的肯定，我们会继续保持！",
            updated_at=datetime.now(),
        )

        service = FeedbackLoopService(db=AsyncMock())
        service.feedback_repo = AsyncMock()
        service.feedback_repo.get_by_id.return_value = feedback
        service.feedback_repo.update.return_value = updated

        result = await service.process_feedback(fb_id)

        assert result["status"] == "resolved"

    @pytest.mark.asyncio
    async def test_feedback_loop_process_not_found(self) -> None:
        """反馈不存在时应抛出 ValueError。"""
        service = FeedbackLoopService(db=AsyncMock())
        service.feedback_repo = AsyncMock()
        service.feedback_repo.get_by_id.return_value = None

        with pytest.raises(ValueError, match="反馈不存在"):
            await service.process_feedback(uuid4())
