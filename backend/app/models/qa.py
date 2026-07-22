"""
问答社区模型 — 单一职责：定义问答帖、回答表。
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class QaQuestion(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """问答帖表 — 企业知识沉淀。"""

    __tablename__ = "qa_questions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="提问者 ID"
    )
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=True, comment="关联知识库"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, comment="租户 ID"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="问题标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="问题详情")
    status: Mapped[str] = mapped_column(
        String(20), default="open", comment="状态: open/answered/closed"
    )
    view_count: Mapped[int] = mapped_column(Integer, default=0, comment="浏览数")
    answer_count: Mapped[int] = mapped_column(Integer, default=0, comment="回答数")
    tags: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="标签（逗号分隔）")

    answers: Mapped[list["QaAnswer"]] = relationship(back_populates="question")


class QaAnswer(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """回答表。"""

    __tablename__ = "qa_answers"

    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_questions.id"), nullable=False, comment="问题 ID"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="回答者 ID"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, comment="租户 ID"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="回答内容")
    is_accepted: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否被采纳")
    is_ai_generated: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否 AI 生成"
    )
    vote_count: Mapped[int] = mapped_column(Integer, default=0, comment="投票数")

    question: Mapped[QaQuestion] = relationship(back_populates="answers")
