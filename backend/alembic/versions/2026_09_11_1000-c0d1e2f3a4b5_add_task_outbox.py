"""add task_outbox table

Revision ID: c0d1e2f3a4b5
Revises: b5c6d7e8f9a0
Create Date: 2026-09-11 10:00:00.000000

P2 轻量 Outbox：一次性触发链路（feedback/qa/智能处理链等）派发失败时
持久化"欠投递"记录，由 flush_task_outbox 定时补投。
PostgreSQL DDL（无 SQLite 兼容）。
"""

from alembic import op

revision: str = "c0d1e2f3a4b5"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_outbox (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            task_name VARCHAR(200) NOT NULL,
            task_kwargs JSONB NOT NULL DEFAULT '{}'::jsonb,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL,
            sent_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_outbox_status ON task_outbox (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_outbox_task_name ON task_outbox (task_name)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_outbox")
