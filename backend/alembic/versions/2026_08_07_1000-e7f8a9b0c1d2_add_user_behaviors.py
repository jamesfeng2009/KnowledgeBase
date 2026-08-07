"""add user_behaviors table

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-08-07 10:00:00.000000

推荐模块（SKILL 第 14 节）Phase 1：

新增 user_behaviors 表 — 记录用户对文档的行为（浏览/收藏/点赞/搜索点击），
作为协同过滤与向量内容召回的信号来源。软删除 + 行级 RLS + 租户隔离。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_behaviors (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            tenant_id UUID,
            user_id UUID NOT NULL REFERENCES users(id),
            doc_id UUID NOT NULL REFERENCES documents(id),
            action_type VARCHAR(20) NOT NULL,
            weight FLOAT NOT NULL DEFAULT 1.0,
            acted_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_user_behavior_identity
                UNIQUE (tenant_id, user_id, doc_id, action_type)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_behavior_user "
        "ON user_behaviors (tenant_id, user_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_behavior_doc "
        "ON user_behaviors (tenant_id, doc_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_behaviors")