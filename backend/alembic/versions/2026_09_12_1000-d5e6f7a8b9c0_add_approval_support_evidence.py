"""add support_evidence to knowledge_approvals

Revision ID: d5e6f7a8b9c0
Revises: e3f4a5b6c7d8
Create Date: 2026-09-12 10:00:00.000000

P2a 沉淀前置闸门：审批记录新增支持度证据列（distillation_gate 统计的
多源好评/采纳信号摘要），供人工审批参考与 D3 自动通过门槛判定。
PostgreSQL DDL（无 SQLite 兼容）。
"""

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE knowledge_approvals
        ADD COLUMN IF NOT EXISTS support_evidence JSONB NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_approvals DROP COLUMN IF EXISTS support_evidence"
    )
