"""候选池模型 — 单一职责：沉淀候选簇（knowledge_candidates 表）ORM。

P3 沉淀候选池：好评/采纳信号先入池归簇（相似问题聚为一簇），
支持度达标后由 beat 晋升任务走既有沉淀+审批流程。

状态机（无物理删除，仅状态流转）：
    candidate  入池待积累
    promoting  晋升中（提取/沉淀/审批提交进行时）
    promoted   已晋升（回填 promoted_asset_id / promoted_doc_id）
    dismissed  管理端手动驳回（dismissed_reason 留痕）
    expired    TTL 内未达标过期

幂等：(source_type, source_id) 在 support_events JSONB 内由
KnowledgeCandidatePool.upsert_from_signal 查重保证（天然抵御 Outbox 重试）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class KnowledgeCandidate(UUIDMixin, TimestampMixin, Base):
    """沉淀候选簇 — 相似好评/采纳信号的归簇容器（id/created_at/updated_at 由 Mixin 提供）。"""

    __tablename__ = "knowledge_candidates"

    asset_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="chat_faq",
        comment="资产类型（当前仅 chat_faq）",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="candidate",
        index=True,
        comment="状态: candidate/promoting/promoted/dismissed/expired",
    )
    representative_question: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="簇代表问题（首个信号的问题文本）",
    )
    question_embedding: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="代表问题嵌入（float 数组，归簇比对用）",
    )
    question_variants: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
        default=list,
        comment="簇内问题变体列表（审计线索）",
    )
    answer_draft: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="采纳路径的答案草稿（晋升时免 LLM 兜底）",
    )
    support_events: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
        default=list,
        comment="支持事件列表 [{source_type, source_id, signal, user_id, message_id, ts}]",
    )
    support_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="加权支持度（praise=1, accepted=2）",
    )
    praise_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="好评事件数"
    )
    accept_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="采纳事件数"
    )
    distinct_users: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="提交信号的不同用户数"
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
        comment="首信号时间（TTL 起点）",
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
        comment="最近信号时间",
    )
    promoted_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="晋升后回填 KnowledgeAsset.id"
    )
    promoted_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="晋升后回填 Document.id"
    )
    dismissed_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="驳回原因（dismissed 状态留痕）"
    )
    # 多租户隔离（SoftDeleteMixin 不适用：候选池无物理删除需求，
    # 过期/驳回均为状态流转）
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
