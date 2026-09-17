"""add document_chunks and chunk_versions tables

P0-2 分块编辑 + 版本历史：
    - document_chunks 存储检索分块的可编辑副本（doc_id + chunk_index 定位）；
    - chunk_versions 存储每次编辑前的快照（version_seq 文档内递增），
      回滚时把版本内容写回 chunk 正文并追加新快照。

PostgreSQL DDL，与项目既有迁移风格一致（无 SQLite 兼容代码）。

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-17 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2b3c4d5e6a7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create document_chunks and chunk_versions tables."""
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "doc_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id"), nullable=False,
        ),
        sa.Column(
            "kb_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id"), nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("section_path", sa.String(length=500), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
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
    op.create_index("ix_document_chunks_doc_id", "document_chunks", ["doc_id"])
    op.create_index("ix_document_chunks_kb_id", "document_chunks", ["kb_id"])

    op.create_table(
        "chunk_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chunk_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_chunks.id"), nullable=False,
        ),
        sa.Column("version_seq", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_text", sa.Text(), nullable=False),
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
    op.create_index("ix_chunk_versions_chunk_id", "chunk_versions", ["chunk_id"])


def downgrade() -> None:
    """Drop chunk_versions and document_chunks tables."""
    op.drop_index("ix_chunk_versions_chunk_id", table_name="chunk_versions")
    op.drop_table("chunk_versions")
    op.drop_index("ix_document_chunks_kb_id", table_name="document_chunks")
    op.drop_index("ix_document_chunks_doc_id", table_name="document_chunks")
    op.drop_table("document_chunks")
