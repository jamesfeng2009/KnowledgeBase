"""
审核流程模型 — 单一职责：定义审核流程表。
"""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class AuditFlow(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """审核流程表 — 文档发布等操作的审核。"""

    __tablename__ = "audit_flows"

    resource_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="资源类型: document/kb/question"
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="资源 ID"
    )
    submitter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="提交者 ID"
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, comment="审核者 ID"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="状态: pending/approved/rejected"
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="审核意见")
    priority: Mapped[str] = mapped_column(
        String(20), default="normal", comment="优先级: low/normal/high"
    )
