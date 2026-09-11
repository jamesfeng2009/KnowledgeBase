"""add research_jobs + dataset export submit idempotency

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-08-20 10:00:00.000000

防超时重试重复建任务（幂等提交）：

1. 新增 research_jobs 表 — Deep Research 每次提交落一条记录，
   job.id 同时作为 Celery task_id 派发。
2. 部分唯一索引守闸门（PostgreSQL 原生 DDL，无 SQLite 兼容）：
   - uq_research_jobs_idem_active ON (user_id, idempotency_key)
     WHERE idempotency_key IS NOT NULL AND status <> 'failed'
     —— 并发双插只有一个赢家；failed 任务不占键，可原键重试；
   - uq_finetune_exports_idem_active 同理守 finetune 导出。
3. finetune_dataset_exports 增加幂等两列（存量 NULL，不参与去重）。
"""

from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS research_jobs (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            user_id UUID NOT NULL REFERENCES users(id),
            tenant_id UUID,
            idempotency_key VARCHAR(128),
            input_hash VARCHAR(64) NOT NULL,
            goal VARCHAR(500) NOT NULL,
            kb_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            status VARCHAR(20) NOT NULL DEFAULT 'queued',
            last_error TEXT
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_research_jobs_idem_active
            ON research_jobs (user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL AND status <> 'failed'
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_jobs_tenant "
        "ON research_jobs (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_jobs_user_created "
        "ON research_jobs (user_id, created_at)"
    )
    op.execute(
        """
        ALTER TABLE finetune_dataset_exports
            ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128),
            ADD COLUMN IF NOT EXISTS input_hash VARCHAR(64)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_finetune_exports_idem_active
            ON finetune_dataset_exports (created_by, idempotency_key)
            WHERE idempotency_key IS NOT NULL AND status <> 'failed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_finetune_exports_idem_active")
    op.execute(
        """
        ALTER TABLE finetune_dataset_exports
            DROP COLUMN IF EXISTS input_hash,
            DROP COLUMN IF EXISTS idempotency_key
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_research_jobs_user_created")
    op.execute("DROP INDEX IF EXISTS ix_research_jobs_tenant")
    op.execute("DROP INDEX IF EXISTS uq_research_jobs_idem_active")
    op.execute("DROP TABLE IF EXISTS research_jobs")
