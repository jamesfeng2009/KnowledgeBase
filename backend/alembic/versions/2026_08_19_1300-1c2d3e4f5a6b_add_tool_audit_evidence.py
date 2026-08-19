"""add result_ref / result_full / evidence_ref to tool_audit_log

Revision ID: 1c2d3e4f5a6b
Revises: 0a1b2c3d4e5f
Create Date: 2026-08-19 13:00:00.000000

P2：工具审计补全完整结果引用（对象存储引用为主 + 关键事件 JSONB 兜底），
evidence_ref 贯穿到审计持久化。PostgreSQL DDL（无 SQLite 兼容）。

- result_ref：完整结果的对象存储引用（artifact ID/路径/URL），普通大结果用；
- result_full：关键审计事件（permission.decision / failure.recover）原文兜底；
- evidence_ref：工具/文档证据引用（P3 贯穿）。

存量数据三列保持 NULL，向后兼容；索引沿用项目既有 ix_* 约定（无新增索引，
按 span_id 反查即可，避免过度索引）。
"""

from alembic import op

revision: str = "1c2d3e4f5a6b"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tool_audit_log
            ADD COLUMN IF NOT EXISTS result_ref VARCHAR(500) NULL,
            ADD COLUMN IF NOT EXISTS result_full JSONB NULL,
            ADD COLUMN IF NOT EXISTS evidence_ref VARCHAR(500) NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE tool_audit_log
            DROP COLUMN IF EXISTS evidence_ref,
            DROP COLUMN IF EXISTS result_full,
            DROP COLUMN IF EXISTS result_ref
        """
    )