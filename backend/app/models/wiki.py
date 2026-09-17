"""
Wiki 模型 — Agent 自动生成的互链 Markdown 知识库（P0-1）。

背景：对齐 WeKnora Wiki 模式 — Agent 从原始文档自动生成结构化、相互链接的
Markdown 知识页面，支持人工编辑、版本历史与一键回滚。

设计约定：
    - wiki_pages：每页对应一篇源文档（source_doc_id 可空，人工页面无源）；
    - wiki_page_versions：每次编辑/回滚前快照（version_seq 页内递增）；
    - wiki_page_links：页面互链（[[标题]] 解析结果），图谱数据源；
    - 生成幂等：同 source_doc_id 重复生成 = 更新页面 + 追加版本。

遵循单一职责：本模块仅定义模型，不包含业务逻辑。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class WikiPage(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Wiki 页面表。"""

    __tablename__ = "wiki_pages"

    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False, index=True,
        comment="所属知识库 ID",
    )
    source_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True, index=True,
        comment="来源文档 ID（人工页面为 NULL）",
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="页面标题")
    content_md: Mapped[str] = mapped_column(Text, nullable=False, comment="Markdown 正文")
    status: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False, comment="状态: draft/published"
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="作者 ID"
    )
    # 多租户隔离 — SaaS 模式下按租户隔离，私有部署为 NULL
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )


class WikiPageVersion(UUIDMixin, TimestampMixin, Base):
    """Wiki 页面版本表 — 每次编辑前的快照。"""

    __tablename__ = "wiki_page_versions"

    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wiki_pages.id"), nullable=False, index=True,
        comment="页面 ID",
    )
    version_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="版本号（页内递增，从 1 开始）"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="版本标题")
    content_md: Mapped[str] = mapped_column(Text, nullable=False, comment="版本 Markdown 快照")
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="操作者 ID"
    )
    summary: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="编辑摘要"
    )


class WikiPageLink(UUIDMixin, TimestampMixin, Base):
    """Wiki 页面互链表 — 图谱节点/边的数据源。"""

    __tablename__ = "wiki_page_links"

    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False, index=True,
        comment="所属知识库 ID",
    )
    from_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wiki_pages.id"), nullable=False, index=True,
        comment="来源页面 ID",
    )
    to_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wiki_pages.id"), nullable=False, index=True,
        comment="目标页面 ID",
    )
    link_text: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="链接文本（[[...]] 内原文）"
    )
