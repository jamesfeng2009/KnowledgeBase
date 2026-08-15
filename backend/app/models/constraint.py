"""约束注入通道数据模型 — constraint_rules / constraint_audit_records。

设计：constraint-recall-design §4。约束是独立实体（非"带标签的文档"）：
- rule_text 人读 + LLM 读；normalized 机器执行（statement / condition /
  required_mentions / forbidden_patterns / amount_limits，§4.1）。
- trigger_entities 供 T2 实体触发器 GIN 匹配；trigger_domains /
  trigger_intents 预留 T1 / T3（Phase 3 接入）。
- 软状态机：pending_review → active → retired（禁 DELETE，Phase 2 打标
  管线与运营手动 INSERT 共用）。
- 版本链 superseded_by 供审计回放（条款修订不覆盖历史）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class ConstraintRule(UUIDMixin, TimestampMixin, Base):
    """约束规则 — 确定性注入的规则条款（一等公民）。"""

    __tablename__ = "constraint_rules"

    # 租户隔离（私有部署 NULL）；查询必须走 apply_tenant_filter
    tenant_id: Mapped[UUID | None] = mapped_column(nullable=True)
    kb_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=False
    )
    # 溯源：条款摘自哪个文档的哪个 chunk（对接图谱 HAS_CHUNK 边）
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id"), nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # global | tenant | kb（冲突裁决分层用，§8）
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="kb")
    # 原文条款（人读 + LLM 读）
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 机器执行定义（§4.1 schema：statement/condition/required_mentions/
    # forbidden_patterns/amount_limits）
    normalized: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # block | confirm | warn（预算分槽与压缩降级按此分级，§7）
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # ⊆ {inject, post_verify, tool_gate} — 三层消费声明（L1/L2/L3）
    actions: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, default=lambda: ["inject"]
    )
    # T1 域触发器（['finance','legal',...]，Phase 3 接入）
    trigger_domains: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, default=list
    )
    # T2 实体触发器（['报销','采购审批',...]，GIN 匹配）
    trigger_entities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    # T3 意图触发器（['RAG_SEARCH',...]，Phase 3 接入）
    trigger_intents: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, default=list
    )
    # 条款级生效窗（覆盖文档级 recency 窗口）
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 版本链 — 修订指向替代条款，审计回放用
    superseded_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("constraint_rules.id"), nullable=True
    )
    # active | pending_review | retired（软状态，禁 DELETE）
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending_review"
    )
    # 自动打标签率（Phase 2 管线产出；人工 INSERT 视为 1.0）
    classifier_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    # ---- 人审记录（P2 · 仿 high_risk_audit_records 复查三字段）----
    # reviewed_at 与 superseded_by 区分「人审退休」（误判证据）与
    # 「版本链退休」（reindex 正常流转），供误判率统计
    reviewed_by: Mapped[UUID | None] = mapped_column(nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # 主查询路径：按 KB 取 active/pending_review 规则（部分索引，
        # 与迁移 DDL 的 WHERE status IN (...) 一致）
        Index(
            "ix_constraint_rules_lookup",
            "kb_id",
            "status",
            postgresql_where=text("status IN ('active', 'pending_review')"),
        ),
        # 人审队列：管理台按 KB 分页拉 pending_review（P2）
        Index(
            "ix_constraint_rules_review",
            "status",
            "kb_id",
            postgresql_where=text("status = 'pending_review'"),
        ),
    )


class ConstraintAuditRecord(UUIDMixin, Base):
    """约束注入决策审计 — 每次路由命中的规则 × 实际处置落一条。

    action 取值：
        injected         实际注入 prompt（enforce 模式）
        skipped_observe  命中但 observe 模式未注入（灰度对比数据）
        filtered_perm    权限链剔除（密级/状态 fail-closed）
        expired          生效窗外剔除
    """

    __tablename__ = "constraint_audit_records"

    tenant_id: Mapped[UUID | None] = mapped_column(nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # 查询原文（应用层截断至 500 字符）
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kb_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    rule_id: Mapped[UUID] = mapped_column(
        ForeignKey("constraint_rules.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # 命中的触发器集合（["T2:entity", "T4:kb_domain"]）
    triggers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
