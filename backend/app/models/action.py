"""
文档行动项模型 — 单一职责：定义文档行动项表。

用于 3.16 文档智能处理中的行动项提取功能，
从会议纪要 / SOP 文档中自动提取 TODO 项。
"""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class DocumentAction(UUIDMixin, TimestampMixin, Base):
    """文档行动项表 — 从文档中提取的 TODO / 行动项。"""

    __tablename__ = "document_actions"

    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id"),
        nullable=False,
        comment="文档 ID",
    )
    assignee: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="负责人"
    )
    deadline: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="截止日期"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="行动内容"
    )
    priority: Mapped[str] = mapped_column(
        String(20), default="medium", comment="优先级: high/medium/low"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="状态: pending/completed"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
