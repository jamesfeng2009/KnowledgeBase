"""
知识缺口仓储 — 单一职责：知识缺口领域的数据访问。

遵循单一职责：KnowledgeGapRepository 只处理 knowledge_gaps 表的查询，
不涉及缺口检测的业务逻辑（委托 GapDetectorService）。

遵循开闭原则：继承 BaseRepository 获得标准 CRUD，
扩展添加缺口专属查询（按优先级、按状态、按主题递增）。

注意：KnowledgeGap 模型不支持软删除（无 SoftDeleteMixin），
因此所有查询不包含 deleted_at 过滤条件。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gap import KnowledgeGap
from app.repositories.base import BaseRepository


class KnowledgeGapRepository(BaseRepository[KnowledgeGap]):
    """知识缺口仓储 — 封装 knowledge_gaps 表的领域查询。

    KnowledgeGap 模型不支持软删除，所有查询不包含 deleted_at 过滤。
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(KnowledgeGap, session)

    async def get_by_topic(self, topic: str) -> KnowledgeGap | None:
        """按主题精确查询缺口（用于去重与计数递增）。

        Args:
            topic: 缺口主题（高频无结果查询词）。
        """
        stmt = select(KnowledgeGap).where(KnowledgeGap.topic == topic)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_all(
        self,
        priority: str | None = None,
        status: str | None = None,
    ) -> list[KnowledgeGap]:
        """查询知识缺口列表（按搜索次数倒序，支持过滤）。

        Args:
            priority: 可选，优先级过滤 — high/medium/low。
            status: 可选，状态过滤 — open/addressed。
        """
        stmt = select(KnowledgeGap)

        if priority is not None:
            stmt = stmt.where(KnowledgeGap.priority == priority)
        if status is not None:
            stmt = stmt.where(KnowledgeGap.status == status)

        stmt = stmt.order_by(KnowledgeGap.search_count.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        id: UUID,
        status: str,
        suggestion: str | None = None,
    ) -> KnowledgeGap | None:
        """更新缺口状态与处理建议。

        Args:
            id: 缺口 ID。
            status: 新状态 — open/addressed。
            suggestion: 处理建议（可选）。
        """
        gap = await self.get_by_id(id)
        if gap is None:
            return None
        gap.status = status
        if suggestion is not None:
            gap.suggestion = suggestion
        await self.session.flush()
        await self.session.refresh(gap)
        return gap

    async def increment_search_count(self, topic: str) -> KnowledgeGap:
        """递增指定主题的搜索次数；不存在则创建新缺口。

        自动根据搜索次数更新优先级：
        - search_count >= 10 → high
        - search_count >= 5  → medium
        - 其余              → low

        Args:
            topic: 缺口主题（查询词）。

        Returns:
            更新或创建后的 KnowledgeGap 实例。
        """
        gap = await self.get_by_topic(topic)
        if gap is None:
            gap = await self.create(topic=topic, search_count=1, priority="low")
        else:
            gap.search_count += 1
            # 根据搜索次数自动调整优先级
            if gap.search_count >= 10:
                gap.priority = "high"
            elif gap.search_count >= 5:
                gap.priority = "medium"
            else:
                gap.priority = "low"
            await self.session.flush()
            await self.session.refresh(gap)
        return gap
