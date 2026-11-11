"""add tickets table (IT helpdesk)

Revision ID: c1d2e3f4a5b6
Revises: b9e4c1d2f3a4
Create Date: 2026-11-10 14:40:00.000000

P1 修复：``frontend/src/pages/scenes/it-helpdesk.astro`` 与
``frontend/src/lib/apis/tickets.ts`` 一直在调 ``/api/v1/tickets``，
但全仓没有任何 router / model / service —— IT 工单场景页整体不可用。

本迁移建 ``tickets`` 表（IT 帮助台工单），与 ``app/models/ticket.py`` 严格对齐。

手写迁移（本仓约定：autogenerate 只用于"看差异"，见 docs/migrations.md）。
"""

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision = "b9e4c1d2f3a4"
branch_labels = None
depends_on = None


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID,
    user_id UUID NOT NULL,
    user_name VARCHAR(100),
    title VARCHAR(500) NOT NULL,
    description TEXT NOT NULL,
    category VARCHAR(30) NOT NULL DEFAULT 'other',
    priority VARCHAR(20) NOT NULL DEFAULT 'medium',
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    ai_answer TEXT,
    ai_solution TEXT,
    ai_solved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
)
"""

_COMMENT_SQL = [
    "COMMENT ON TABLE tickets IS 'IT 帮助台工单'",
    "COMMENT ON COLUMN tickets.tenant_id IS '租户 ID（多租户隔离，私有部署可空）'",
    "COMMENT ON COLUMN tickets.user_id IS '提交人 ID'",
    "COMMENT ON COLUMN tickets.user_name IS '提交人姓名快照（用户删除后仍可读）'",
    "COMMENT ON COLUMN tickets.title IS '工单标题'",
    "COMMENT ON COLUMN tickets.description IS '问题描述'",
    "COMMENT ON COLUMN tickets.category IS '分类: account/network/software/hardware/access/other'",
    "COMMENT ON COLUMN tickets.priority IS '优先级: low/medium/high/urgent'",
    "COMMENT ON COLUMN tickets.status IS '状态: open/progress/resolved'",
    "COMMENT ON COLUMN tickets.ai_answer IS 'AI 给出的解答原文（用户采纳时回填）'",
    "COMMENT ON COLUMN tickets.ai_solution IS '结构化解决方案 / 处理步骤'",
    "COMMENT ON COLUMN tickets.ai_solved IS '是否由 AI 解答解决'",
]

_FK_SQL = [
    "ALTER TABLE tickets ADD CONSTRAINT fk_tickets_tenant_id "
    "FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE SET NULL",
    "ALTER TABLE tickets ADD CONSTRAINT fk_tickets_user_id "
    "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
]

_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_tickets_tenant_status ON tickets (tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_created_at ON tickets (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_deleted_at ON tickets (deleted_at)",
]


def upgrade() -> None:
    op.execute(_TABLE_SQL)
    for stmt in _COMMENT_SQL:
        op.execute(stmt)
    for stmt in _FK_SQL:
        op.execute(stmt)
    for stmt in _INDEX_SQL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tickets")
