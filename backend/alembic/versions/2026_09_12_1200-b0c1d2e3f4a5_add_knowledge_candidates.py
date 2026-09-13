"""add knowledge_candidates table

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-09-12 12:00:00.000000

P3 沉淀候选池：信号先进池归簇，支持度达标才晋升进既有沉淀+审批流程。
状态机：candidate / promoting / promoted / dismissed / expired（无物理删除）。
幂等：(source_type, source_id) 查重在 support_events JSONB 内由 Service 层保证，
不建唯一索引（Outbox 重试不产生重复计数）。PostgreSQL DDL。
"""

from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID,
    asset_type VARCHAR(30) NOT NULL DEFAULT 'chat_faq',
    status VARCHAR(20) NOT NULL DEFAULT 'candidate',
    representative_question TEXT NOT NULL,
    question_embedding JSONB,
    question_variants JSONB DEFAULT '[]',
    answer_draft TEXT,
    support_events JSONB DEFAULT '[]',
    support_count INT NOT NULL DEFAULT 0,
    praise_count INT NOT NULL DEFAULT 0,
    accept_count INT NOT NULL DEFAULT 0,
    distinct_users INT NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_asset_id UUID,
    promoted_doc_id UUID,
    dismissed_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_candidates_tenant_status "
    "ON knowledge_candidates (tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_candidates_last_seen "
    "ON knowledge_candidates (last_seen_at)",
]


def upgrade() -> None:
    op.execute(_TABLE_SQL)
    for stmt in _INDEX_SQL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_candidates")
