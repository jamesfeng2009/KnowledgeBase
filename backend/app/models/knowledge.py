"""
知识库与文档模型 — 单一职责：定义知识库、文档、文档版本表。
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class KnowledgeBase(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """知识库表。"""

    __tablename__ = "knowledge_bases"

    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="知识库名称")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="描述")
    visibility: Mapped[str] = mapped_column(
        String(20), default="private", comment="可见性: public/private/dept"
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="所有者 ID"
    )
    dept_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True, comment="部门 ID"
    )
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="标签列表")

    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base")


class Document(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """文档表。"""

    __tablename__ = "documents"

    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False, comment="知识库 ID"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="标题")
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True, comment="HTML 内容")
    content_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="Tiptap JSON")
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="纯文本（检索用）")
    doc_type: Mapped[str] = mapped_column(
        String(20), default="md", comment="文档类型: md/html/docx/pdf"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="draft", comment="状态: draft/published/archived"
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="所有者 ID"
    )
    dept_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True, comment="部门 ID"
    )
    classification: Mapped[str] = mapped_column(
        String(20), default="internal", comment="密级: public/internal/confidential/secret"
    )
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="原始文件路径")
    yjs_update: Mapped[bytes | None] = mapped_column(nullable=True, comment="Yjs 二进制状态")
    view_count: Mapped[int] = mapped_column(Integer, default=0, comment="浏览次数")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="AI 自动摘要")
    category: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="AI 自动分类: 政策/SOP/技术文档/会议纪要/培训资料/产品文档/合同模板"
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document")


class DocumentVersion(UUIDMixin, TimestampMixin, Base):
    """文档版本表 — 协同编辑历史快照。"""

    __tablename__ = "document_versions"

    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, comment="文档 ID"
    )
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True, comment="HTML 快照")
    content_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="JSON 快照")
    yjs_update: Mapped[bytes | None] = mapped_column(nullable=True, comment="Yjs 二进制状态")
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="操作者 ID"
    )
    summary: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="版本摘要")

    document: Mapped[Document] = relationship(back_populates="versions")
