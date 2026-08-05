"""add effective_from/effective_to validity window to documents

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-04 10:00:00.000000

P0-4 检索层时间新鲜度加权（新旧规范冲突场景）：

1. documents 表新增 effective_from / effective_to 生效窗口列
   （规范类文档可选，NULL = 永久有效，向后兼容）
2. 检索层 recency.filter_by_validity_window 据此做硬过滤；
   updated_at（TimestampMixin 已有）用于平局裁决，无需新增列
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE documents "
        "ADD COLUMN IF NOT EXISTS effective_from TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE documents "
        "ADD COLUMN IF NOT EXISTS effective_to TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS effective_to")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS effective_from")
