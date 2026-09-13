"""课题调研任务模型 — 单一职责：定义 Deep Research 提交记录表。

幂等提交（防超时重试重复建任务）：
    - 同一用户 + 同一 idempotency_key 只允许存在一条"活跃"任务；
    - 部分唯一索引排除 failed 状态 —— 失败的任务不占键，可原键重试；
    - input_hash 记录提交内容指纹，同键不同输入返回 409 而非悄悄丢弃。

job.id 同时作为 Celery task_id 派发，`/research/{task_id}/stream|result`
无需感知两种编号。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class ResearchJob(UUIDMixin, TimestampMixin, Base):
    """课题调研任务表 — 一次提交意图一条记录。"""

    __tablename__ = "research_jobs"
    __table_args__ = (
        Index(
            "uq_research_jobs_idem_active",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status <> 'failed'"
            ),
        ),
        Index("ix_research_jobs_tenant", "tenant_id"),
        Index("ix_research_jobs_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="提交用户 ID（幂等键归属范围）",
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离/观测）"
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="幂等键（调用方生成，随重试保持不变；NULL=不参与去重）",
    )
    input_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="提交内容指纹（goal + kb_ids 规范化 JSON 的 SHA-256）",
    )
    goal: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="调研目标"
    )
    kb_ids: Mapped[list] = mapped_column(
        JSONB, default=list, comment="权限收敛后的知识库范围"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="queued",
        nullable=False,
        comment="状态: queued/success/failed（执行中仍为 queued，进度看 Redis 流）",
    )
    output_json: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="最终报告（status=success 时与状态同一条 UPDATE 写入，DB 为权威存储）",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="终态写入时间（success/failed 时记录）",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败原因（status=failed 时记录）"
    )
