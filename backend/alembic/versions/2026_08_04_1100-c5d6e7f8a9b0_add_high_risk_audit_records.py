"""add high_risk_audit_records table

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-04 11:00:00.000000

P1-8 高风险信息三档分级 + 审计：

1. 新增 high_risk_audit_records 表 — 记录 action="block" 的拦截决策
   （query / answer 快照 / 核验明细 JSONB / 三档分级结果）
2. review_status 支持管理员复查标记（pending/confirmed/misjudged），
   误判率统计用于反哺分级阈值
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS high_risk_audit_records (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            query TEXT NOT NULL,
            answer_snippet TEXT NOT NULL,
            session_id VARCHAR(64) NOT NULL,
            user_id UUID,
            tenant_id UUID,
            total_count INTEGER NOT NULL,
            unverified_count INTEGER NOT NULL,
            max_risk_level VARCHAR(16) NOT NULL,
            items JSONB NOT NULL DEFAULT '[]'::jsonb,
            review_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            reviewed_by UUID,
            reviewed_at TIMESTAMPTZ,
            review_comment TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_high_risk_audit_records_session_id "
        "ON high_risk_audit_records (session_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_high_risk_audit_records_review_status "
        "ON high_risk_audit_records (review_status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_high_risk_audit_records_tenant_id "
        "ON high_risk_audit_records (tenant_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS high_risk_audit_records")
