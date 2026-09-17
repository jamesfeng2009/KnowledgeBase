"""add kb_folders table

P0-3 文件夹树：知识库显式文件夹节点。

与 Document.path（"产品/合规/数据安全"）共用同一套 path 约定：
    - path 为 '/'-分隔字符串，无前导斜杠；
    - depth 为分段数（根文件夹=1）；
    - 软删除，Service 层保证 (kb_id, path) 唯一（软删下不建 DB 唯一约束）。

PostgreSQL DDL，与项目既有迁移风格一致（无 SQLite 兼容代码）。

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-09-17 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create kb_folders table."""
    op.create_table(
        "kb_folders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "kb_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.String(length=1000), nullable=False),
        sa.Column("parent_path", sa.String(length=1000), nullable=True),
        sa.Column(
            "depth", sa.Integer(), nullable=False, server_default="1",
            comment="层级深度（根文件夹=1）",
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default="0",
            comment="同级排序",
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
    op.create_index("ix_kb_folders_kb_id", "kb_folders", ["kb_id"])
    op.create_index("ix_kb_folders_path", "kb_folders", ["path"])


def downgrade() -> None:
    """Drop kb_folders table."""
    op.drop_index("ix_kb_folders_path", table_name="kb_folders")
    op.drop_index("ix_kb_folders_kb_id", table_name="kb_folders")
    op.drop_table("kb_folders")
