"""
分块模型 — 检索分块的持久化与版本历史（P0-2）。

背景：分块（Chunk）原本是内存 dataclass，随向量库 upsert 后不再可寻址。
本模块将分块落库，支持"可视化编辑 + 快照版本 + diff/回滚 + 重建索引"。

设计约定：
    - document_chunks 是文档分块的事实来源（正文编辑后重索引以此为准）；
    - chunk_versions 记录每次编辑前的旧内容快照（版本号递增），
      回滚 = 把指定版本内容写回 chunk 正文并生成新的快照版本；
    - 存量文档首次访问分块时惰性初始化（init），不迁移存量向量数据。

遵循单一职责：本模块仅定义模型，不包含业务逻辑。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class DocumentChunk(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """文档分块表 — 检索分块的可编辑持久化副本。"""

    __tablename__ = "document_chunks"

    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True,
        comment="所属文档 ID",
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False, index=True,
        comment="所属知识库 ID",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="分块序号（文档内顺序）"
    )
    section_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="标题路径锚点（P3），如 '产品 > 概述'"
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False, comment="分块正文（可编辑）")
    token_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="估算 token 数"
    )
    # 多租户隔离 — SaaS 模式下按租户隔离，私有部署为 NULL
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )


class ChunkVersion(UUIDMixin, TimestampMixin, Base):
    """分块版本表 — 每次编辑前的旧内容快照。"""

    __tablename__ = "chunk_versions"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_chunks.id"), nullable=False, index=True,
        comment="分块 ID",
    )
    version_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="版本号（文档内递增，从 1 开始）"
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False, comment="版本正文快照")
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="操作者 ID"
    )
    summary: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="编辑摘要"
    )
