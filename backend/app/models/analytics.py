"""
分析日志模型 — 单一职责：定义搜索日志表。

用于 3.17 知识健康度仪表盘，记录每次搜索行为，
支撑搜索热词、零点击查询、知识覆盖率等运营指标。
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class SearchLog(UUIDMixin, TimestampMixin, Base):
    """搜索日志表 — 记录每次用户搜索行为。

    用于仪表盘的搜索热词分析、零点击查询分析、知识覆盖率计算。
    """

    __tablename__ = "search_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="用户 ID（匿名搜索时为空）",
    )
    query: Mapped[str] = mapped_column(
        Text, nullable=False, comment="搜索关键词"
    )
    source: Mapped[str] = mapped_column(
        String(50), default="knowledge_base", comment="搜索源: knowledge_base/oa/erp/crm"
    )
    result_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="返回结果数"
    )
    clicked: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否点击了结果"
    )
    clicked_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id"),
        nullable=True,
        comment="点击的文档 ID",
    )
