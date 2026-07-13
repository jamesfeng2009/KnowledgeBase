"""
知识缺口模型 — 单一职责：定义知识缺口表。

知识缺口检测的产物：当用户搜索频繁返回无结果时，
系统自动记录为缺口，用于驱动知识库内容补充。
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class KnowledgeGap(UUIDMixin, TimestampMixin, Base):
    """知识缺口表 — 记录高频无结果查询，驱动内容补充。

    数据来源：
    - 用户搜索返回空结果时，由 GapDetectorService.record_no_result 记录；
    - 相同 topic 的查询累积 search_count，达到阈值后提升 priority。

    生命周期：
    - open：缺口待处理（需补充知识或提供替代答案）；
    - addressed：已处理（补充了文档或标记为无需处理）。
    """

    __tablename__ = "knowledge_gaps"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=True,
        comment="租户 ID（SaaS 模式下隔离租户数据）",
    )
    topic: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="缺口主题（高频无结果查询词）"
    )
    search_count: Mapped[int] = mapped_column(
        Integer, default=1, comment="该主题的无结果搜索次数"
    )
    priority: Mapped[str] = mapped_column(
        String(20),
        default="low",
        comment="优先级: high/medium/low（由搜索频率决定）",
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="缺口描述（自动生成或人工补充）"
    )
    suggestion: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="处理建议（人工填写）"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="open",
        comment="状态: open/addressed",
    )
