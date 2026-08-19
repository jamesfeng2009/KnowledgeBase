"""memory_fact source fields: traceability binding (P0-1)

Revision ID: 0a1b2c3d4e5f
Revises: e2f3a4b5c6d7
Create Date: 2026-08-19

记忆溯源（P0-1）：让每条记忆事实都能回溯到它的来源。
- source_type：来源类型 message/document/tool/feedback
- source_ref_id：来源引用 ID（消息/文档/工具结果 ID），加索引供按来源反查
- raw_excerpt：原始摘录文本，供溯源时的原话核验

存量数据 source_type 等保持 NULL（视为"旧数据不可溯源"），向后兼容。
索引命名复用项目既有 ix_memory_facts_* 约定。

PostgreSQL DDL（项目硬约束：无 SQLite 兼容代码，不用 batch_alter_table）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0a1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_facts
            ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) NULL,
            ADD COLUMN IF NOT EXISTS source_ref_id UUID NULL,
            ADD COLUMN IF NOT EXISTS raw_excerpt TEXT NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_facts_source_type "
        "ON memory_facts (source_type)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_facts_source_ref_id "
        "ON memory_facts (source_ref_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_facts_source_ref_id")
    op.execute("DROP INDEX IF EXISTS ix_memory_facts_source_type")
    op.execute(
        """
        ALTER TABLE memory_facts
            DROP COLUMN IF EXISTS raw_excerpt,
            DROP COLUMN IF EXISTS source_ref_id,
            DROP COLUMN IF EXISTS source_type
        """
    )