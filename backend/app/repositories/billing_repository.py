"""
计费与租户仓储 — 单一职责：SaaS 多租户计费领域的数据访问。

遵循单一职责：
- TenantRepository 只处理租户表的查询；
- UsageRecordRepository 只处理用量记录表的查询。

遵循开闭原则：
- 两者均继承 BaseRepository 获得标准 CRUD；
- 扩展添加各自领域的专属查询方法。

注意：
- Tenant 支持软删除，查询自动过滤 deleted_at IS NULL；
- UsageRecord 不支持软删除（无 SoftDeleteMixin），查询不包含该过滤。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Tenant, UsageRecord
from app.repositories.base import BaseRepository


class TenantRepository(BaseRepository[Tenant]):
    """租户仓储 — 封装租户表的领域查询。"""

    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        super().__init__(Tenant, session, tenant_id=tenant_id)

    async def get_by_domain(self, domain: str) -> Tenant | None:
        """根据域名查询租户（排除已软删除）。

        域名用于 SaaS 多租户路由：根据请求 Host 头解析租户。
        """
        stmt = select(Tenant).where(
            Tenant.domain == domain,
            Tenant.deleted_at.is_(None),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()


class UsageRecordRepository(BaseRepository[UsageRecord]):
    """用量记录仓储 — 封装用量记录表的领域查询。

    UsageRecord 模型不支持软删除，所有查询不包含 deleted_at 过滤。
    """

    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        super().__init__(UsageRecord, session, tenant_id=tenant_id)

    async def get_by_tenant(
        self,
        tenant_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[UsageRecord]:
        """查询指定租户在时间范围内的用量记录。

        Args:
            tenant_id: 租户 ID。
            start_date: 起始时间（包含，>=）。
            end_date: 结束时间（不包含，<）。

        时间范围使用 [start_date, end_date) 半开区间，避免跨日统计重复。

        租户隔离说明：本方法已通过显式参数 ``tenant_id`` 过滤
        （UsageRecord.tenant_id == tenant_id），无需再追加 _apply_all_filters，
        避免与 self._tenant_id 重复过滤。
        """
        stmt = (
            select(UsageRecord)
            .where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.created_at >= start_date,
                UsageRecord.created_at < end_date,
            )
            .order_by(UsageRecord.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_total_cost(self, tenant_id: UUID) -> int:
        """统计指定租户的累计成本（单位：分）。

        对 cost_cents 字段求和。若无用量记录，返回 0。
        返回 int 类型（分），由上层转换为元展示。

        租户隔离说明：本方法已通过显式参数 ``tenant_id`` 过滤
        （UsageRecord.tenant_id == tenant_id），无需再追加 _apply_all_filters，
        避免与 self._tenant_id 重复过滤。
        """
        stmt = select(func.sum(UsageRecord.cost_cents)).where(
            UsageRecord.tenant_id == tenant_id,
        )
        result = await self.session.scalar(stmt)
        return int(result) if result is not None else 0
