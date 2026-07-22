"""
记忆层 ORM 模型 — 统一归 models 层管理，避免循环导入。

包含三个表：
  memory_facts        — Mem0 当前事实存储（KV + Embedding）
  graphiti_entities   — Graphiti 知识实体（时间线追踪）
  graphiti_events     — Graphiti 实体变更事件（历史记录）

遵循单一职责：本文件仅定义 ORM 模型，不含业务逻辑。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class MemoryFact(UUIDMixin, TimestampMixin, Base):
    """Mem0 事实表 — 存储跨会话的用户事实和偏好。

    用途：高频缓存、用户偏好、工作记忆。
    特点：KV + Embedding 双索引，支持语义检索和精确匹配。
    """

    __tablename__ = "memory_facts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True, comment="用户 ID"
    )
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="类别: preference/working/summary/entity"
    )
    fact_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="事实内容（自然语言）"
    )
    fact_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="结构化键（可选，用于精确查询）"
    )
    fact_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="结构化值（可选）"
    )
    embedding: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="向量嵌入（语义检索用）"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否有效（软删除标记）"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="过期时间（NULL=永不过期）"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )


class KnowledgeEntity(UUIDMixin, TimestampMixin, Base):
    """知识实体表 — 追踪知识条目的生命周期。

    用途：知识时间线、实体关系演化、知识过期预警。
    特点：图 + 时间区间，记录事实的"有效时间段"。
    """

    __tablename__ = "graphiti_entities"

    entity_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="实体类型: document/concept/policy/product",
    )
    entity_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=True,
        comment="关联的业务 ID（如文档 ID）",
    )
    name: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="实体名称"
    )
    current_version: Mapped[str] = mapped_column(
        String(50), default="v1", comment="当前版本"
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="生效时间"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="失效时间（NULL=当前有效）",
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )


class EntityEvent(UUIDMixin, TimestampMixin, Base):
    """实体事件表 — 记录实体的变更历史。

    与 KnowledgeEntity 的关系：一个实体有多个事件，形成时间线。
    valid_to 为 NULL 表示当前生效的事件。
    """

    __tablename__ = "graphiti_events"

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("graphiti_entities.id"),
        nullable=False,
        comment="实体 ID",
    )
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="事件类型: version_updated/status_changed/expired/deprecated/merged/split",
    )
    old_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="旧值"
    )
    new_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="新值"
    )
    event_source: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="事件来源"
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="生效时间"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="失效时间"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
