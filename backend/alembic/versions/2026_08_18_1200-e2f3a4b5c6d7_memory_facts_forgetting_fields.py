"""memory_facts forgetting fields: access stats + supersede markers

Revision ID: e2f3a4b5c6d7
Revises: f0e1d2c3b4a5
Create Date: 2026-08-18

记忆的第三种动作 — 遗忘（课程 07《敢遗忘才是解药》）：
- access_count / last_accessed_at：激活值（ACT-R 三因子）的频率与
  近期增益数据源，召回命中写回。
- superseded_by / superseded_at：机制二（写入时增量冲突整合）的败者
  标记。is_active=False 只表达"不活跃"，superseded_* 专指"被新记忆
  语义覆写"，二者解耦后 P2 软删除窗口（误判复活）才有判定依据。

PostgreSQL DDL（项目硬约束：无 SQLite 兼容代码）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "f0e1d2c3b4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_facts
            ADD COLUMN IF NOT EXISTS access_count INT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS superseded_by UUID NULL,
            ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_facts
            DROP COLUMN IF EXISTS superseded_at,
            DROP COLUMN IF EXISTS superseded_by,
            DROP COLUMN IF EXISTS last_accessed_at,
            DROP COLUMN IF EXISTS access_count
        """
    )
