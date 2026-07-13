"""
知识缺口检测服务 — 单一职责：检测、记录与处理知识缺口。

知识缺口 = 用户高频搜索但返回空结果的查询主题。
通过记录无结果搜索并按频率聚类，识别知识库内容缺失，
驱动内容补充策略。

遵循单一职责：GapDetectorService 只负责缺口检测与状态管理，
不涉及文档创建（委托 KnowledgeService）或反馈处理（委托 FeedbackLoopService）。
遵循开闭原则：缺口优先级判定策略由 Repository 的 increment_search_count
实现，Service 层不关心具体阈值。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.gap_repository import KnowledgeGapRepository
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 高频阈值 — 搜索次数达到此值被视为高频缺口。
HIGH_FREQUENCY_THRESHOLD: int = 5


class GapDetectorService:
    """知识缺口检测服务 — 检测高频无结果查询并管理缺口。

    使用方式::

        service = GapDetectorService(db)
        await service.record_no_result("报销流程怎么走")
        gaps = await service.get_gaps(priority="high")
        await service.address_gap(gap_id, suggestion="已补充报销流程文档")
    """

    def __init__(self, db: AsyncSession) -> None:
        """初始化知识缺口检测服务。

        Args:
            db: 异步数据库会话。
        """
        self.db: AsyncSession = db
        self.gap_repo: KnowledgeGapRepository = KnowledgeGapRepository(db)

    # ------------------------------------------------------------------
    # 记录无结果查询
    # ------------------------------------------------------------------

    async def record_no_result(self, query: str) -> None:
        """记录一次无结果搜索。

        若该查询主题已存在，递增搜索次数并自动更新优先级；
        若不存在，创建新的缺口记录（初始优先级为 low）。

        Args:
            query: 返回空结果的搜索关键词。
        """
        if not query or not query.strip():
            return

        topic = query.strip()
        gap = await self.gap_repo.increment_search_count(topic)
        log.info(
            "gap.recorded",
            topic=topic,
            search_count=gap.search_count,
            priority=gap.priority,
        )

    # ------------------------------------------------------------------
    # 检测缺口
    # ------------------------------------------------------------------

    async def detect_gaps(self) -> list[dict[str, Any]]:
        """检测高频无结果查询，返回缺口列表。

        筛选条件：search_count >= HIGH_FREQUENCY_THRESHOLD 的 open 状态缺口。
        结果按搜索次数倒序排列。

        Returns:
            高频缺口详情列表。
        """
        gaps = await self.gap_repo.get_all(status="open")
        high_freq_gaps = [
            g for g in gaps if g.search_count >= HIGH_FREQUENCY_THRESHOLD
        ]

        result = [
            {
                "id": str(gap.id),
                "topic": gap.topic,
                "search_count": gap.search_count,
                "priority": gap.priority,
                "status": gap.status,
                "description": gap.description,
                "created_at": gap.created_at.isoformat() if gap.created_at else None,
            }
            for gap in high_freq_gaps
        ]
        log.info(
            "gap.detected",
            total_open=len(gaps),
            high_frequency=len(high_freq_gaps),
        )
        return result

    # ------------------------------------------------------------------
    # 查询缺口
    # ------------------------------------------------------------------

    async def get_gaps(
        self,
        priority: str | None = None,
    ) -> list[dict[str, Any]]:
        """获取知识缺口列表（可按优先级过滤）。

        Args:
            priority: 可选，优先级过滤 — high/medium/low。

        Returns:
            缺口详情列表（按搜索次数倒序）。
        """
        gaps = await self.gap_repo.get_all(priority=priority)
        return [
            {
                "id": str(gap.id),
                "topic": gap.topic,
                "search_count": gap.search_count,
                "priority": gap.priority,
                "status": gap.status,
                "description": gap.description,
                "suggestion": gap.suggestion,
                "created_at": gap.created_at.isoformat() if gap.created_at else None,
            }
            for gap in gaps
        ]

    # ------------------------------------------------------------------
    # 处理缺口
    # ------------------------------------------------------------------

    async def address_gap(
        self,
        gap_id: uuid.UUID,
        suggestion: str,
    ) -> dict[str, Any]:
        """标记知识缺口已处理。

        将缺口状态从 open 变更为 addressed，并记录处理建议。

        Args:
            gap_id: 缺口 ID。
            suggestion: 处理建议（如"已补充XX流程文档"）。

        Returns:
            更新后的缺口详情。

        Raises:
            ValueError: 缺口不存在。
        """
        gap = await self.gap_repo.update_status(
            gap_id,
            status="addressed",
            suggestion=suggestion,
        )
        if gap is None:
            raise ValueError(f"知识缺口不存在: {gap_id}")

        log.info(
            "gap.addressed",
            gap_id=str(gap_id),
            suggestion=suggestion,
        )
        return {
            "id": str(gap.id),
            "topic": gap.topic,
            "status": gap.status,
            "suggestion": gap.suggestion,
            "updated_at": gap.updated_at.isoformat() if gap.updated_at else None,
        }
