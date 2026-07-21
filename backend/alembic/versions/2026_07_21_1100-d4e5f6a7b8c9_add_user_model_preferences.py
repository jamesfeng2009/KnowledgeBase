"""add user_model_preferences table for P2 model selection

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-21 11:00:00.000000

P2: 用户模型选择 — 存储会话级模型偏好，支持两级优先级：
    session 级（本表）> system 默认（models.json is_default）
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 user_model_preferences 表。"""
    op.create_table(
        "user_model_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
            comment="用户 ID",
        ),
        sa.Column(
            "session_id",
            sa.String(100),
            nullable=False,
            index=True,
            comment="会话 ID",
        ),
        sa.Column(
            "model_id",
            sa.String(100),
            nullable=False,
            comment="模型 ID（引用 models.json）",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="租户 ID（多租户预留）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "session_id", name="uq_user_session_model"
        ),
    )


def downgrade() -> None:
    """删除 user_model_preferences 表。"""
    op.drop_table("user_model_preferences")
