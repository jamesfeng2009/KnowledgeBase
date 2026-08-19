"""add verdict_reason / forgotten_reason / activation_value to memory_facts

Revision ID: a4b5c6d7e8f9
Revises: 1c2d3e4f5a6b
Create Date: 2026-08-19 14:00:00.000000

P1：仲裁裁决理由与遗忘原因/激活值快照落库，「为什么」可审计。
PostgreSQL DDL（无 SQLite 兼容）。

- verdict_reason：写入时仲裁理由（ConsolidateVerdict.reason 枚举），记录
  这条记忆是经何裁决落库的；
- forgotten_reason：退场原因（superseded_conflict/dedup/corrected/expired），
  记录软删除时为何退场；
- activation_value：退场瞬间 ACT-R 激活值快照，支撑激活值衰减的审计与复现。

存量数据三列保持 NULL（视为"旧数据无原因记录"），向后兼容。
"""

from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision = "1c2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_facts
            ADD COLUMN IF NOT EXISTS verdict_reason VARCHAR(64) NULL,
            ADD COLUMN IF NOT EXISTS forgotten_reason VARCHAR(64) NULL,
            ADD COLUMN IF NOT EXISTS activation_value FLOAT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_facts
            DROP COLUMN IF EXISTS activation_value,
            DROP COLUMN IF EXISTS forgotten_reason,
            DROP COLUMN IF EXISTS verdict_reason
        """
    )