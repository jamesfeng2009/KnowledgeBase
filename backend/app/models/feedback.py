"""
用户反馈模型 — 单一职责：定义用户反馈表。
"""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Feedback(UUIDMixin, TimestampMixin, Base):
    """用户反馈表 — 反馈闭环层。"""

    __tablename__ = "feedbacks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="用户 ID"
    )
    type: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="类型: bug/suggestion/praise/complaint"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="反馈内容")
    status: Mapped[str] = mapped_column(
        String(20), default="open", comment="状态: open/processing/resolved/closed"
    )
    priority: Mapped[str] = mapped_column(
        String(20), default="normal", comment="优先级: low/normal/high/urgent"
    )
    related_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True, comment="关联消息 ID"
    )
    response: Mapped[str | None] = mapped_column(Text, nullable=True, comment="处理回复")
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
