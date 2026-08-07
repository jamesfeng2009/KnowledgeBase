"""
用户行为模型 — 单一职责：定义推荐模块的用户行为记录表。

行为是推荐的唯一信号来源：协同过滤（UserCF/ItemCF）与向量内容召回
（用户偏好向量）都基于行为聚合。行为按动作类型加权，并要求租户隔离。

遵循惯例：软删除 + 行级 RLS + 租户隔离（与文档/知识库一致）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin

#: 行为类型 → 权重（近期行为由查询侧近因加权）
ACTION_WEIGHTS: dict[str, float] = {
    "view": 1.0,
    "search_click": 1.5,
    "collect": 2.0,
    "like": 3.0,
}


class UserBehavior(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """用户行为表 — 记录用户对文档的浏览/收藏/点赞/搜索点击。

    同一 (tenant_id, user_id, doc_id, action_type) 唯一，多次行为通过
    weight 累加（upsert），避免行数无限膨胀。
    """

    __tablename__ = "user_behaviors"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "doc_id",
            "action_type",
            name="uq_user_behavior_identity",
        ),
        Index("ix_user_behavior_user", "tenant_id", "user_id", "created_at"),
        Index("ix_user_behavior_doc", "tenant_id", "doc_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="用户 ID"
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, comment="文档 ID"
    )
    action_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="行为类型: view/search_click/collect/like"
    )
    weight: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, comment="行为权重（可累加）"
    )
    acted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="行为发生时间"
    )