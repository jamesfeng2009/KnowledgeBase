"""add wiki_pages, wiki_page_versions, wiki_page_links tables

P0-1 Wiki 模式：Agent 从原始文档自动生成互链 Markdown 知识库。

PostgreSQL DDL，与项目既有迁移风格一致（无 SQLite 兼容代码）。

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-17 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3c4d5e6a7b8"
down_revision: Union[str, Sequence[str], None] = "f2b3c4d5e6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create wiki_pages, wiki_page_versions, wiki_page_links tables."""
    op.create_table(
        "wiki_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "kb_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id"), nullable=False,
        ),
        sa.Column(
            "source_doc_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id"), nullable=True,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column(
            "author_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wiki_pages_kb_id", "wiki_pages", ["kb_id"])
    op.create_index("ix_wiki_pages_source_doc_id", "wiki_pages", ["source_doc_id"])

    op.create_table(
        "wiki_page_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "page_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wiki_pages.id"), nullable=False,
        ),
        sa.Column("version_seq", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column(
            "author_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("summary", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_wiki_page_versions_page_id", "wiki_page_versions", ["page_id"])

    op.create_table(
        "wiki_page_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "kb_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id"), nullable=False,
        ),
        sa.Column(
            "from_page_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wiki_pages.id"), nullable=False,
        ),
        sa.Column(
            "to_page_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wiki_pages.id"), nullable=False,
        ),
        sa.Column("link_text", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_wiki_page_links_kb_id", "wiki_page_links", ["kb_id"])
    op.create_index("ix_wiki_page_links_from_page_id", "wiki_page_links", ["from_page_id"])
    op.create_index("ix_wiki_page_links_to_page_id", "wiki_page_links", ["to_page_id"])


def downgrade() -> None:
    """Drop wiki tables (逆序删外键依赖）。"""
    op.drop_index("ix_wiki_page_links_to_page_id", table_name="wiki_page_links")
    op.drop_index("ix_wiki_page_links_from_page_id", table_name="wiki_page_links")
    op.drop_index("ix_wiki_page_links_kb_id", table_name="wiki_page_links")
    op.drop_table("wiki_page_links")
    op.drop_index("ix_wiki_page_versions_page_id", table_name="wiki_page_versions")
    op.drop_table("wiki_page_versions")
    op.drop_index("ix_wiki_pages_source_doc_id", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_kb_id", table_name="wiki_pages")
    op.drop_table("wiki_pages")
