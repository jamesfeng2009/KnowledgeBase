"""
高风险拦截审计模型 — P1-8：block 决策落审计表，支持定期复查误判率反哺阈值。

设计要点：
    - 仅记录 action="block" 的决策（warn/confirm 量级大且无需复查）；
    - items 以 JSONB 保存完整核验明细（含三档分级 risk_level 与偏差幅度）；
    - review_status 支持管理员复查标记：pending → confirmed（确认误判为正确拦截）
      / misjudged（误判），误判率统计用于反哺分级阈值调整。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class HighRiskAuditRecord(UUIDMixin, TimestampMixin, Base):
    """高风险拦截审计表 — 记录 block 决策的完整上下文。"""

    __tablename__ = "high_risk_audit_records"
    __table_args__ = (
        Index("ix_high_risk_audit_records_session_id", "session_id"),
        Index("ix_high_risk_audit_records_review_status", "review_status"),
        Index("ix_high_risk_audit_records_tenant_id", "tenant_id"),
    )

    # ---- 决策上下文 ----
    query: Mapped[str] = mapped_column(
        Text, nullable=False, comment="用户问题"
    )
    answer_snippet: Mapped[str] = mapped_column(
        Text, nullable=False, comment="被阻断答案（截断至 2000 字）"
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="会话 ID"
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="用户 ID"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # ---- 核验结果 ----
    total_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="检测到的高风险信息总数"
    )
    unverified_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="未核验通过的信息数"
    )
    max_risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="最高风险等级: low/medium/high"
    )
    items: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list,
        comment="核验明细（含 risk_level 与 deviation）",
    )

    # ---- 复查（反哺阈值） ----
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
        comment="复查状态: pending/confirmed/misjudged",
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="复查人 ID"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="复查时间"
    )
    review_comment: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="复查意见"
    )
