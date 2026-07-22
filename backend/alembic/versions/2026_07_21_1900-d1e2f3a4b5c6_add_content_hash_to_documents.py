"""add content_hash to documents

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-07-21 19:00:00

P1-B: 文档内容哈希 — SHA-256(纯文本内容)，用于：
    1. 跨知识库查重（上传时检测重复内容）
    2. 增量更新（内容未变则跳过重新分块和向量化）
    3. 幂等写入（确定性 chunk ID 依赖内容哈希）
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """添加 content_hash 列 + 索引。"""
    # P1-B: documents 加 content_hash 列
    op.add_column(
        "documents",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )

    # 索引 — 加速查重查询（WHERE content_hash = ?）
    op.create_index(
        "ix_documents_content_hash",
        "documents",
        ["content_hash"],
    )


def downgrade() -> None:
    """回滚 content_hash 列。"""
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.drop_column("documents", "content_hash")
