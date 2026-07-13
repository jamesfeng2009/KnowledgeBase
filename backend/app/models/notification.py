"""
通知模型 — 单一职责：定义知识推送通知表。

三种推送策略的统一存储：
    - personal_digest: 个性化知识日报
    - document_change: 文档变更通知
    - gap_alert: 知识缺口预警
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Notification(UUIDMixin, TimestampMixin, Base):
    """通知表 — 知识主动推送的存储实体。

    推送渠道：站内通知（WebSocket 实时）、邮件、IM Bot。
    通知类型：personal_digest / document_change / gap_alert。
    """

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="接收用户 ID"
    )
    notification_type: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="通知类型: personal_digest/document_change/gap_alert"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="通知标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="通知内容")
    doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True, comment="关联文档 ID"
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已读")
    read_at: Mapped[str | None] = mapped_column(
        String(30), nullable=True, comment="已读时间 ISO 格式"
    )
