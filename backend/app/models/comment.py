"""
文档评论模型 — 单一职责：定义文档评论表。
"""

import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class DocumentComment(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """文档评论表 — 异步讨论（非 IM 实时聊天）。"""

    __tablename__ = "document_comments"

    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, comment="文档 ID"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="评论者 ID"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="评论内容")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_comments.id"), nullable=True, comment="父评论 ID"
    )
    resolved: Mapped[bool] = mapped_column(default=False, comment="是否已解决")
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    replies: Mapped[list["DocumentComment"]] = relationship(
        back_populates="parent"
    )
    parent: Mapped["DocumentComment | None"] = relationship(
        back_populates="replies", remote_side="DocumentComment.id"
    )
