"""add tool_audit_log table

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-05 10:00:00.000000

Agent 评测体系 Phase 1（评测.md §4.4）：

新增 tool_audit_log 表 — 持久化关键 Span（tool.call / permission.decision /
failure.recover），供安全审计与离线评测回溯。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_audit_log (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            run_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(64) NOT NULL,
            span_id VARCHAR(32) NOT NULL,
            tool_name VARCHAR(128) NOT NULL,
            arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
            result_summary TEXT NOT NULL DEFAULT '',
            error TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'success'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_audit_log_run_id "
        "ON tool_audit_log (run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_audit_log_session_id "
        "ON tool_audit_log (session_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tool_audit_log")
