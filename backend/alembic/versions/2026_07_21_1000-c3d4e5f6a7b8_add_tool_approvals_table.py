"""add tool_approvals table for P1 approval persistence

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-21 10:00:00.000000

P1: 工具审批持久化 — 当 DangerousToolGuard 拦截危险工具时，
将审批请求持久化到 tool_approvals 表，支持：
- 前端通过 REST 端点审批/拒绝
- 服务重启后恢复未决审批（标记过期 + 加载活跃）
- AgentState JSONB 快照，支持审批后恢复 Agent Loop

表结构：
- id: UUID 主键
- user_id: 用户 ID（FK → users.id）
- session_id: 会话 ID（索引，用于会话级查询）
- tenant_id: 租户 ID（多租户预留，NULL）
- tool_name: 被拦截的工具名称
- tool_use_id: LLM 返回的 tool_use ID
- tool_arguments: 工具调用参数（JSONB）
- reason: 拦截原因（展示给用户）
- irreversible: 是否为不可逆操作
- agent_state_snapshot: AgentState 快照（JSONB，恢复时使用）
- status: 审批状态（pending/approved/rejected/expired）
- resolved_at: 审批处理时间
- resolved_by: 审批处理人
- expire_at: 过期时间（默认 1 小时）
- created_at / updated_at: 时间戳
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 tool_approvals 表。"""
    op.create_table(
        "tool_approvals",
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
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="租户 ID（多租户预留）",
        ),
        sa.Column(
            "tool_name",
            sa.String(100),
            nullable=False,
            comment="被拦截的工具名称",
        ),
        sa.Column(
            "tool_use_id",
            sa.String(200),
            nullable=False,
            comment="LLM 返回的 tool_use ID",
        ),
        sa.Column(
            "tool_arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
            comment="工具调用参数",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
            comment="拦截原因（展示给用户）",
        ),
        sa.Column(
            "irreversible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否为不可逆操作",
        ),
        sa.Column(
            "agent_state_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
            comment="AgentState 快照（JSONB，恢复时使用）",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            index=True,
            comment="审批状态: pending/approved/rejected/expired",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="审批处理时间",
        ),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="审批处理人",
        ),
        sa.Column(
            "expire_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="过期时间（默认 1 小时）",
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
    )

    # 复合索引：用户 + 状态（查询用户的 pending 审批）
    op.create_index(
        "ix_tool_approvals_user_status",
        "tool_approvals",
        ["user_id", "status"],
    )

    # 索引：过期时间（启动时扫描过期审批）
    op.create_index(
        "ix_tool_approvals_expire_at",
        "tool_approvals",
        ["expire_at"],
    )


def downgrade() -> None:
    """删除 tool_approvals 表。"""
    op.drop_index("ix_tool_approvals_expire_at", table_name="tool_approvals")
    op.drop_index("ix_tool_approvals_user_status", table_name="tool_approvals")
    op.drop_table("tool_approvals")
