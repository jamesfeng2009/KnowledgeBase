"""
用例评审服务 — 单一职责：测试用例评审的提交、查询与审批流转。

复用 AuditFlow 的 pending/approved/rejected 工作流模式，但独立存储
以支持测试特有的评审建议（suggestions）和评审摘要（review_summary）。

关键设计：
    - 状态联动：提交评审时用例状态 → pending_review；
      审批通过 → approved；驳回 → draft。
    - 评审摘要：审批/驳回时根据 suggestions 自动生成 review_summary，
      便于评审列表快速浏览。
    - 软删除感知：查询用例时过滤 deleted_at IS NULL，避免评审已删除用例。

使用方式::

    service = TestReviewService(db, current_user)
    review = await service.submit_for_review(case_id, comment="请尽快评审")
    pending = await service.get_pending_reviews(page=1, size=20)
    approved = await service.approve(review_id, comment="通过", suggestions=[...])
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.testing import TestCase, TestReview
from app.models.user import User
from app.utils.logger import get_logger

log = get_logger(__name__)

# 可提交评审的用例状态白名单
_SUBMITTABLE_STATUSES: frozenset[str] = frozenset({"draft", "active"})


class TestReviewService:
    """用例评审服务 — 评审提交、查询与审批/驳回流转。

    使用 ``AsyncSession`` 直接操作 ORM，事务由 ``get_db_session`` 依赖统一管理。
    通过 ``User`` 注入当前操作者，用于填充 submitter_id / reviewer_id。

    使用方式::

        service = TestReviewService(db, current_user)
        review = await service.submit_for_review(case_id, comment="初版用例")
        pending = await service.get_pending_reviews(page=1, size=20)
        approved = await service.approve(review_id, comment="通过")
    """

    def __init__(self, db: AsyncSession, user: User) -> None:
        """初始化评审服务。

        Args:
            db: 异步数据库会话，事务由 ``get_db_session`` 依赖统一管理。
            user: 当前操作用户，用于填充 submitter_id / reviewer_id。
        """
        self.db: AsyncSession = db
        self.user: User = user

    # ------------------------------------------------------------------
    # 提交评审
    # ------------------------------------------------------------------

    async def submit_for_review(
        self,
        case_id: uuid.UUID,
        comment: str | None = None,
    ) -> TestReview:
        """提交用例评审 — 将用例状态从 draft/active 变更为 pending_review。

        校验用例存在性、软删除状态和当前状态白名单后，创建评审记录
        （status=pending），并联动更新用例状态为 pending_review。

        Args:
            case_id: 用例 ID。
            comment: 评审备注（可选，提交者的附加说明）。

        Returns:
            创建后的 ``TestReview`` 对象（status 为 pending）。

        Raises:
            ValueError: 用例不存在、已软删除或状态不在白名单中。
        """
        case = await self._get_case(case_id)
        if case is None:
            raise ValueError(f"测试用例不存在: {case_id}")
        if case.status not in _SUBMITTABLE_STATUSES:
            raise ValueError(
                f"用例当前状态为 {case.status}，仅 {_SUBMITTABLE_STATUSES} 状态可提交评审"
            )

        review = TestReview(
            case_id=case_id,
            submitter_id=self.user.id,
            status="pending",
            comment=comment,
        )
        self.db.add(review)

        # 联动更新用例状态为 pending_review
        await self.db.execute(
            update(TestCase)
            .where(TestCase.id == case_id)
            .values(status="pending_review")
        )
        await self.db.flush()

        log.info(
            "test_review.submitted",
            review_id=str(review.id),
            case_id=str(case_id),
            submitter=str(self.user.id),
        )
        return review

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def get_pending_reviews(
        self,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[TestReview], int]:
        """分页查询待评审列表 — 按 created_at 降序排列。

        Args:
            page: 页码，从 1 开始。
            size: 每页数量。

        Returns:
            ``(reviews, total)`` — 当前页评审列表与总记录数。
        """
        # 总数
        count_stmt = select(func.count()).select_from(TestReview).where(
            TestReview.status == "pending"
        )
        total = (await self.db.execute(count_stmt)).scalar_one()

        # 分页数据
        offset = (page - 1) * size
        stmt = (
            select(TestReview)
            .where(TestReview.status == "pending")
            .order_by(TestReview.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        result = await self.db.execute(stmt)
        reviews = list(result.scalars().all())
        return reviews, total

    async def get_review(self, review_id: uuid.UUID) -> TestReview | None:
        """按 ID 查询评审记录。

        Args:
            review_id: 评审 ID。

        Returns:
            ``TestReview`` 或 ``None``（不存在时）。
        """
        result = await self.db.execute(
            select(TestReview).where(TestReview.id == review_id)
        )
        return result.scalar_one_or_none()

    async def get_reviews_by_case(self, case_id: uuid.UUID) -> list[TestReview]:
        """查询某用例的全部评审记录 — 按 created_at 降序排列。

        Args:
            case_id: 用例 ID。

        Returns:
            评审记录列表（可能为空）。
        """
        stmt = (
            select(TestReview)
            .where(TestReview.case_id == case_id)
            .order_by(TestReview.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 审批 / 驳回
    # ------------------------------------------------------------------

    async def approve(
        self,
        review_id: uuid.UUID,
        comment: str | None = None,
        suggestions: list[dict] | None = None,
    ) -> TestReview:
        """通过评审 — 将评审状态变更为 approved，联动用例状态为 approved。

        更新评审记录的 reviewer_id、resolved_at、comment、suggestions，
        并根据 suggestions 生成 review_summary。用例状态同步变更为 approved。

        Args:
            review_id: 评审 ID。
            comment: 评审意见（可选）。
            suggestions: 评审建议列表（可选），每项为 dict。

        Returns:
            更新后的 ``TestReview`` 对象（status 为 approved）。

        Raises:
            ValueError: 评审不存在或已处理（非 pending 状态）。
        """
        return await self._resolve_review(
            review_id=review_id,
            review_status="approved",
            case_status="approved",
            comment=comment,
            suggestions=suggestions,
        )

    async def reject(
        self,
        review_id: uuid.UUID,
        comment: str | None = None,
        suggestions: list[dict] | None = None,
    ) -> TestReview:
        """驳回评审 — 将评审状态变更为 rejected，用例状态回退为 draft。

        更新评审记录的 reviewer_id、resolved_at、comment、suggestions，
        并根据 suggestions 生成 review_summary。用例状态同步回退为 draft。

        Args:
            review_id: 评审 ID。
            comment: 驳回原因（可选，建议提供）。
            suggestions: 评审建议列表（可选），每项为 dict。

        Returns:
            更新后的 ``TestReview`` 对象（status 为 rejected）。

        Raises:
            ValueError: 评审不存在或已处理（非 pending 状态）。
        """
        return await self._resolve_review(
            review_id=review_id,
            review_status="rejected",
            case_status="draft",
            comment=comment,
            suggestions=suggestions,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _get_case(self, case_id: uuid.UUID) -> TestCase | None:
        """获取用例 ORM 实例 — 过滤软删除记录。"""
        stmt = select(TestCase).where(
            TestCase.id == case_id,
            TestCase.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _resolve_review(
        self,
        review_id: uuid.UUID,
        review_status: str,
        case_status: str,
        comment: str | None,
        suggestions: list[dict] | None,
    ) -> TestReview:
        """统一处理审批/驳回流转 — 更新评审记录与联动用例状态。

        Args:
            review_id: 评审 ID。
            review_status: 目标评审状态（approved / rejected）。
            case_status: 目标用例状态（approved / draft）。
            comment: 评审意见。
            suggestions: 评审建议列表。

        Returns:
            更新后的 ``TestReview`` 对象。

        Raises:
            ValueError: 评审不存在或已处理（非 pending 状态）。
        """
        review = await self.get_review(review_id)
        if review is None:
            raise ValueError(f"评审记录不存在: {review_id}")
        if review.status != "pending":
            raise ValueError(f"评审已处理，当前状态: {review.status}")

        review_summary = self._build_review_summary(review_status, suggestions, comment)
        now = datetime.now(timezone.utc)

        await self.db.execute(
            update(TestReview)
            .where(TestReview.id == review_id)
            .values(
                status=review_status,
                reviewer_id=self.user.id,
                resolved_at=now,
                comment=comment,
                suggestions=suggestions,
                review_summary=review_summary,
            )
        )

        # 联动更新用例状态
        await self.db.execute(
            update(TestCase)
            .where(TestCase.id == review.case_id)
            .values(status=case_status)
        )
        await self.db.flush()

        # 刷新 ORM 实例以反映更新后的字段值
        await self.db.refresh(review)

        log.info(
            "test_review.resolved",
            review_id=str(review_id),
            status=review_status,
            case_status=case_status,
            reviewer=str(self.user.id),
        )
        return review

    @staticmethod
    def _build_review_summary(
        status: str,
        suggestions: list[dict] | None,
        comment: str | None,
    ) -> str:
        """根据评审状态与建议生成评审摘要。

        Args:
            status: 评审状态（approved / rejected）。
            suggestions: 评审建议列表。
            comment: 评审意见。

        Returns:
            评审摘要文本。
        """
        parts: list[str] = []
        if status == "approved":
            parts.append("评审通过")
        else:
            parts.append("评审驳回")

        if suggestions:
            suggestion_count = len(suggestions)
            parts.append(f"共 {suggestion_count} 条建议")
            # 提取建议摘要（取前 3 条的 type/suggestion 字段）
            for item in suggestions[:3]:
                if isinstance(item, dict):
                    desc = item.get("suggestion") or item.get("type") or ""
                    if desc:
                        parts.append(f"- {desc}")

        if comment:
            parts.append(f"意见: {comment}")

        return "; ".join(parts)
