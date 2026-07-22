"""
审核流程仓储 — 单一职责：审核流程领域的数据访问。

遵循单一职责：AuditRepository 只处理 audit_flows 表的查询，
不涉及审核的业务编排（委托 AuditService）。

遵循开闭原则：继承 BaseRepository 获得标准 CRUD，
扩展添加审核专属查询（按状态、按资源、按提交者）。

注意：AuditFlow 模型支持软删除（SoftDeleteMixin），
BaseRepository 的查询方法自动过滤 deleted_at IS NULL。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditFlow
from app.repositories.base import BaseRepository


class AuditRepository(BaseRepository[AuditFlow]):
    """审核流程仓储 — 封装 audit_flows 表的领域查询。

    AuditFlow 模型支持软删除，所有查询自动过滤 deleted_at IS NULL。
    AuditFlow 已新增 tenant_id 列，BaseRepository 的标准 CRUD 方法
    通过 _apply_all_filters 自动注入租户过滤。自定义查询方法
    （get_by_status / get_by_resource / get_by_submitter / get_by_reviewer）
    通过 _apply_all_filters 手动注入租户隔离条件。
    """

    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        super().__init__(AuditFlow, session, tenant_id=tenant_id)

    async def get_by_status(self, status: str) -> list[AuditFlow]:
        """按状态查询审核列表（按创建时间倒序，排除已软删除）。

        Args:
            status: 审核状态 — pending/approved/rejected。
        """
        stmt = self._apply_all_filters(
            select(AuditFlow)
            .where(
                AuditFlow.status == status,
                AuditFlow.deleted_at.is_(None),
            )
            .order_by(AuditFlow.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_resource(
        self,
        resource_type: str,
        resource_id: UUID,
    ) -> list[AuditFlow]:
        """按资源类型与 ID 查询审核记录（排除已软删除）。

        Args:
            resource_type: 资源类型 — document/kb/question。
            resource_id: 资源 ID。
        """
        stmt = self._apply_all_filters(
            select(AuditFlow)
            .where(
                AuditFlow.resource_type == resource_type,
                AuditFlow.resource_id == resource_id,
                AuditFlow.deleted_at.is_(None),
            )
            .order_by(AuditFlow.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_submitter(self, submitter_id: UUID) -> list[AuditFlow]:
        """查询某用户提交的所有审核记录（排除已软删除，按创建时间倒序）。

        Args:
            submitter_id: 提交者用户 ID。
        """
        stmt = self._apply_all_filters(
            select(AuditFlow)
            .where(
                AuditFlow.submitter_id == submitter_id,
                AuditFlow.deleted_at.is_(None),
            )
            .order_by(AuditFlow.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_reviewer(self, reviewer_id: UUID) -> list[AuditFlow]:
        """查询某审核者处理过的审核记录（排除已软删除，按创建时间倒序）。

        Args:
            reviewer_id: 审核者用户 ID。
        """
        stmt = self._apply_all_filters(
            select(AuditFlow)
            .where(
                AuditFlow.reviewer_id == reviewer_id,
                AuditFlow.deleted_at.is_(None),
            )
            .order_by(AuditFlow.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
