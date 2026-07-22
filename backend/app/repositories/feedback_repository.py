"""
用户反馈仓储 — 单一职责：用户反馈领域的数据访问。

遵循单一职责：FeedbackRepository 只处理反馈表的查询，
不涉及反馈的邮件通知或工单系统对接。

遵循开闭原则：继承 BaseRepository 获得标准 CRUD，
扩展添加反馈专属查询（按状态、按用户）。

注意：Feedback 模型不支持软删除（无 SoftDeleteMixin），
因此所有查询不包含 deleted_at 过滤条件。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback
from app.repositories.base import BaseRepository


class FeedbackRepository(BaseRepository[Feedback]):
    """用户反馈仓储 — 封装反馈表的领域查询。

    Feedback 模型不支持软删除，所有查询不包含 deleted_at 过滤。
    """

    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        super().__init__(Feedback, session, tenant_id=tenant_id)

    async def get_by_status(self, status: str) -> list[Feedback]:
        """按状态查询反馈列表（按创建时间倒序）。

        Args:
            status: 反馈状态 — open/processing/resolved/closed。
        """
        stmt = select(Feedback).where(Feedback.status == status)
        stmt = self._apply_all_filters(stmt)
        stmt = stmt.order_by(Feedback.created_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_user(self, user_id: UUID) -> list[Feedback]:
        """查询某用户提交的所有反馈（按创建时间倒序）。"""
        stmt = select(Feedback).where(Feedback.user_id == user_id)
        stmt = self._apply_all_filters(stmt)
        stmt = stmt.order_by(Feedback.created_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
