"""知识回流审批模型 — 单一职责：定义知识审批表。

P2 回流审批工作流：防止低质/敏感内容污染知识库。
好评反馈/采纳答案沉淀的 FAQ 默认进入 pending_review 状态，
经自动检测分流（高质量自动通过）或人工审批后才能 active + published。

复用 ApprovalService 模式（pending → approved/rejected + 过期机制）。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class KnowledgeApproval(UUIDMixin, TimestampMixin, Base):
    """知识回流审批表 — FAQ 沉淀后的审批流转。

    生命周期：pending → approved / rejected / expired

    自动检测分流（submit_for_review 时执行）：
        - quality_score >= CHAT_FAQ_AUTO_APPROVE_THRESHOLD (默认 0.9)
        - conflict_count == 0
        - pii_detected == False
        → 自动 approve（asset.status=active, doc.status=published）
        否则 → pending（人工审批）

    关联：
        - asset_id → knowledge_assets.id（沉淀的 FAQ 资产）
        - doc_id → documents.id（沉淀的 FAQ 文档）
        - kb_id → knowledge_bases.id（目标知识库）
        - reviewer_id → users.id（审批人，自动通过时为 NULL）
    """

    __tablename__ = "knowledge_approvals"

    # 关联的知识资产
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_assets.id"),
        nullable=False,
        index=True,
        comment="关联的知识资产 ID",
    )
    # 关联的文档（沉淀的 FAQ Document）
    doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id"),
        nullable=True,
        index=True,
        comment="关联的文档 ID",
    )
    # 目标知识库
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id"),
        nullable=False,
        index=True,
        comment="目标知识库 ID",
    )
    # 审批人（自动通过时为 NULL）
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="审批人 ID（自动通过时为 NULL）",
    )
    # 审批状态: pending / approved / rejected / expired
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True,
        comment="审批状态: pending/approved/rejected/expired",
    )
    # === 自动检测结果（辅助审批决策）===
    # LLM 质量评分（0.0~1.0，来自 _llm_extract_faq 的 confidence）
    quality_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="LLM 质量评分（0.0~1.0）"
    )
    # PII 检测命中
    pii_detected: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="PII 检测是否命中"
    )
    # 冲突数量（来自 _detect_conflicts_for_assets）
    conflict_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="检测到的冲突数量"
    )
    # 自动检测详情（PII 命中位置/冲突描述等）
    auto_detected_risks: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="自动检测风险详情列表"
    )
    # === 审批元数据 ===
    # 过期时间（pending 状态超时自动 expired）
    expire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="审批过期时间"
    )
    # 审批时间
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="审批时间"
    )
    # 审批备注（拒绝原因 / 通过说明）
    review_note: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="审批备注"
    )
    # 是否自动通过
    auto_approved: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否自动通过"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
