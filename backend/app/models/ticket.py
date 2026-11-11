"""
IT 工单模型 — 单一职责：定义 IT 帮助台工单表。

场景（``/scenes/it-helpdesk``）：员工提交 IT 问题工单，AI 先给一版解答，
员工可以「采纳并关闭」——采纳时把 AI 解答写进 ``ai_answer`` 并置
``status=resolved`` / ``ai_solved=True``；不采纳则留 open 等人工处理。

设计取舍：
    - ``user_name`` 冗余存快照：工单列表要显示提交人，而提交人可能离职被删除，
      实时 join 会显示空白；快照保证历史工单可读。
    - 状态机只允许 open → progress → resolved，以及 open → resolved（AI 直接解决）；
      resolved 不可回退（要复开就新建工单，保留问题生命周期可审计）。
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class Ticket(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """IT 工单表。"""

    __tablename__ = "tickets"
    __table_args__ = (
        # 列表默认按状态过滤 + 时间倒序；复合索引覆盖"某状态下的最新工单"
        Index("ix_tickets_tenant_status", "tenant_id", "status"),
        Index("ix_tickets_created_at", "created_at"),
        # 所有列表查询都带 deleted_at IS NULL，软删除过滤走索引避免全表扫
        Index("ix_tickets_deleted_at", "deleted_at"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", name="fk_tickets_tenant_id", ondelete="SET NULL"),
        nullable=True,
        comment="租户 ID（多租户隔离，私有部署可空）",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_tickets_user_id", ondelete="CASCADE"),
        nullable=False,
        comment="提交人 ID",
    )
    user_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="提交人姓名快照（用户删除后仍可读）"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="工单标题")
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="问题描述")
    category: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="other",
        comment="分类: account/network/software/hardware/access/other",
    )
    priority: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="medium",
        comment="优先级: low/medium/high/urgent",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="open",
        comment="状态: open/progress/resolved",
    )
    ai_answer: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="AI 给出的解答原文（用户采纳时回填）"
    )
    ai_solution: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="结构化解决方案 / 处理步骤"
    )
    ai_solved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否由 AI 解答解决"
    )
