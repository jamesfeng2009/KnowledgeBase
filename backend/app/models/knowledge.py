"""
知识库与文档模型 — 单一职责：定义知识库、文档、文档版本表。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
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
    # 多租户隔离 — SaaS 模式下按租户隔离知识库，私有部署为 NULL
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

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
        String(20), default="draft", comment="状态: draft/pending_review/published/archived"
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
    file_size: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="文件大小（字节），用于 GB 视频分流判断"
    )
    # P1-B: 内容哈希 — SHA-256(纯文本内容)，用于跨知识库查重和增量更新
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="内容 SHA-256 哈希（去重 + 增量更新）"
    )
    yjs_update: Mapped[bytes | None] = mapped_column(nullable=True, comment="Yjs 二进制状态")
    view_count: Mapped[int] = mapped_column(Integer, default=0, comment="浏览次数")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="AI 自动摘要")
    category: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="AI 自动分类: 政策/SOP/技术文档/会议纪要/培训资料/产品文档/合同模板"
    )
    # === 解析元数据（P1 增强：解析任务产物持久化）===
    # parse_status 区别于 status：status 表示业务状态（draft/published），
    # parse_status 表示解析质量状态（parsed/partial/failed/pending），
    # 文档已 published 但解析部分失败时，status=published 但 parse_status=partial
    parse_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="解析状态: parsed/partial/failed/pending"
    )
    parse_warnings: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="解析警告列表（解析/向量化/索引失败信息）"
    )
    page_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="页数/幻灯片数/工作表数"
    )
    char_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="正文字符数"
    )
    # 多租户隔离 — SaaS 模式下按租户隔离文档，私有部署为 NULL
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )
    # === P0-4 生效窗口（规范类文档）===
    # 检索层硬过滤：窗口外文档不进入候选；NULL = 永久有效（向后兼容）
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="生效时间（NULL=立即生效）"
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="失效时间（NULL=永久有效）"
    )
    # === P0 wiki 层级元数据（系列/子 wiki/子文档）===
    # 存储扁平化 + 元数据编码层级 — 检索时按 filters 过滤，非多跳存储。
    # 所有字段 nullable：旧文档无层级信息时不影响检索（向后兼容）。
    # 多跳（沿引用链追溯 N 跳）留给 Neo4j GraphService，不在本次范围。
    series_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True, comment="所属系列 ID（同系列文档共享）"
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True, comment="父文档 ID"
    )
    path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True, index=True, comment="层级路径 '产品/合规/数据安全'"
    )
    depth: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="层级深度（根=0）"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="同级排序"
    )
    version_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True,
        comment="版本族主文档 ID（v2 的 version_of 指向 v1）"
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document")
    # P0 wiki 层级：子文档 + 版本族反向关系（便于 ORM 查询层级树）
    children: Mapped[list["Document"]] = relationship(
        "Document",
        foreign_keys=[parent_id],
        back_populates="parent",
    )
    parent: Mapped["Document | None"] = relationship(
        "Document",
        foreign_keys=[parent_id],
        back_populates="children",
        remote_side="Document.id",
    )
    version_children: Mapped[list["Document"]] = relationship(
        "Document",
        foreign_keys=[version_of],
        back_populates="version_master",
    )
    version_master: Mapped["Document | None"] = relationship(
        "Document",
        foreign_keys=[version_of],
        back_populates="version_children",
        remote_side="Document.id",
    )


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
