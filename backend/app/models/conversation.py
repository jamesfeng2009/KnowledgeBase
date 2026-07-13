"""
对话与消息模型 — 单一职责：定义对话、消息表。
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class Conversation(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """对话表 — AI 问答会话。"""

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="用户 ID"
    )
    title: Mapped[str] = mapped_column(String(255), default="新对话", comment="对话标题")
    agent_type: Mapped[str] = mapped_column(
        String(50), default="qa", comment="Agent 类型: qa/workflow/action"
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.created_at"
    )


class Message(UUIDMixin, TimestampMixin, Base):
    """消息表 — 对话中的单条消息。"""

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, comment="对话 ID"
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="角色: user/assistant/system"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息内容")
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="引用来源")
    token_count: Mapped[int] = mapped_column(Integer, default=0, comment="Token 消耗")
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="使用的模型")

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
