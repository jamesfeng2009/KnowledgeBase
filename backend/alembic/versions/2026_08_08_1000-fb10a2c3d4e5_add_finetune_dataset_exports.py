"""add finetune_dataset_exports table

Revision ID: fb10a2c3d4e5
Revises: f8a9b0c1d2e3
Create Date: 2026-08-08 10:00:00.000000

微调数据飞轮：

新增 finetune_dataset_exports 表 — 记录每一次微调数据集（SFT/DPO/Embedding/Golden）
的版本化导出：状态流转（pending/building/completed/failed）、样本数、过滤统计、
JSONL 文件路径与大小、构建参数与 Celery 任务 ID。多租户隔离（tenant_id 索引）。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fb10a2c3d4e5"
down_revision: Union[str, None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS finetune_dataset_exports (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            tenant_id UUID,
            dataset_type VARCHAR(20) NOT NULL,
            version VARCHAR(40) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            sample_count INTEGER NOT NULL DEFAULT 0,
            filtered_stats JSONB NOT NULL DEFAULT '{}'::jsonb,
            file_path VARCHAR(500),
            file_size_bytes BIGINT NOT NULL DEFAULT 0,
            celery_task_id VARCHAR(64),
            created_by UUID REFERENCES users(id),
            completed_at TIMESTAMPTZ,
            params JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_finetune_dataset_exports_tenant "
        "ON finetune_dataset_exports (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_finetune_dataset_exports_type "
        "ON finetune_dataset_exports (tenant_id, dataset_type)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS finetune_dataset_exports")
