"""research_jobs 增加权威结果列 output_json / finished_at

Revision ID: e3f4a5b6c7d8
Revises: c0d1e2f3a4b5
Create Date: 2026-09-11 11:00:00.000000

结果权威落库（P0-1）：
    最终报告原先仅存 Redis（TTL 24h），Redis 重启/驱逐后 research_jobs
    仍为 success 而结果不可查。本次新增：
    - output_json JSONB — status=success 时与状态在同一条 UPDATE 写入，
      成为权威存储（Redis 降级为查询缓存与自愈来源）；
    - finished_at TIMESTAMPTZ — 终态写入时间。
    存量行两列为 NULL：success 旧行在下次查询时由 Redis 结果回填自愈。
"""

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE research_jobs
            ADD COLUMN IF NOT EXISTS output_json JSONB,
            ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE research_jobs
            DROP COLUMN IF EXISTS finished_at,
            DROP COLUMN IF EXISTS output_json
        """
    )
