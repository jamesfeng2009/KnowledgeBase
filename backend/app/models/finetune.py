"""
微调数据集导出模型 — 单一职责：定义微调数据集导出记录表。

每条记录对应一次数据集版本化导出（SFT/DPO/Embedding/Golden），
生命周期：pending → building → completed / failed。
构建产物为 JSONL 文件（路径与大小落库），样本级统计存 filtered_stats。
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class DatasetExport(UUIDMixin, TimestampMixin, Base):
    """微调数据集导出记录表。"""

    __tablename__ = "finetune_dataset_exports"
    __table_args__ = (
        Index("ix_finetune_dataset_exports_tenant", "tenant_id"),
        Index("ix_finetune_dataset_exports_type", "tenant_id", "dataset_type"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
    dataset_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="数据集类型: sft/dpo/embedding/golden"
    )
    version: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="版本号: v{YYYYMMDD-HHmmss}"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="状态: pending/building/completed/failed"
    )
    sample_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="导出样本数"
    )
    filtered_stats: Mapped[dict] = mapped_column(
        JSONB, default=dict, comment="过滤统计（按原因分类: classification/duplicate/...）"
    )
    file_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="JSONL 文件路径"
    )
    file_size_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, comment="文件大小（字节）"
    )
    celery_task_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="构建任务 Celery ID"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, comment="创建者 ID"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    params: Mapped[dict] = mapped_column(
        JSONB, default=dict, comment="构建参数（max_classification/days/min_rating/limit）"
    )
