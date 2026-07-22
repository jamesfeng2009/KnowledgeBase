"""add tenant_id checkpoint table and usage metadata

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-19 14:00:00.000000

P0-P2 模型字段缺口修复统一迁移：

P0 修复:
- knowledge_bases / documents 表新增 tenant_id（多租户隔离）
- 新建 agent_checkpoints 表（LangGraph Agent Loop 状态持久化）

P1 修复:
- notifications.read_at 类型从 String(30) 改为 DateTime(timezone=True)
- 10 个表新增 tenant_id（memory_facts/knowledge_entities/entity_events/
  conversations/messages/document_actions/search_logs/feedbacks/
  document_comments/notifications）
- usage_records 新增 duration_ms / success / request_id
- subscriptions 新增 status / billing_cycle / seats / cancelled_at /
  auto_renew / metadata_

向后兼容：所有新增字段均有默认值或允许 NULL，历史数据无影响。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """P0-P2 统一升级。"""

    # ============================================================
    # P0-1: knowledge_bases / documents 新增 tenant_id
    # ============================================================
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "tenant_id",
            sa.UUID(),
            nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "tenant_id",
            sa.UUID(),
            nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
    )

    # ============================================================
    # P0-3: 新建 agent_checkpoints 表
    # ============================================================
    op.create_table(
        "agent_checkpoints",
        sa.Column(
            "session_id",
            sa.String(length=64),
            nullable=False,
            comment="会话 ID（对应 Conversation ID）",
        ),
        sa.Column(
            "agent_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Agent 完整状态 JSON",
        ),
        sa.Column(
            "iteration",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="当前迭代次数",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
            comment="最后更新时间",
        ),
        sa.PrimaryKeyConstraint("session_id"),
        comment="Agent Loop 状态检查点表",
    )

    # ============================================================
    # P1-3: notifications.read_at 类型从 String(30) 改为 DateTime
    # ============================================================
    # 历史数据清理：移除非时间格式的脏数据
    op.execute(
        "UPDATE notifications SET read_at = NULL "
        "WHERE read_at IS NOT NULL AND read_at !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'"
    )

    # 类型转换：PostgreSQL USING 子句
    op.alter_column(
        "notifications",
        "read_at",
        existing_type=sa.String(length=30),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="read_at::timestamp with time zone",
        comment="已读时间（UTC 时间戳）",
    )

    # ============================================================
    # P1-4: 10 个表新增 tenant_id
    # ============================================================
    _add_tenant_id_columns = [
        ("memory_facts", "记忆事实"),
        ("graphiti_entities", "知识实体"),
        ("graphiti_events", "实体事件"),
        ("conversations", "对话"),
        ("messages", "消息"),
        ("document_actions", "文档行动项"),
        ("search_logs", "搜索日志"),
        ("feedbacks", "用户反馈"),
        ("document_comments", "文档评论"),
        ("notifications", "通知"),
    ]
    for table_name, _desc in _add_tenant_id_columns:
        op.add_column(
            table_name,
            sa.Column(
                "tenant_id",
                sa.UUID(),
                nullable=True,
                comment="租户 ID（多租户隔离）",
            ),
        )

    # ============================================================
    # P1-5: usage_records 新增 duration_ms / success / request_id
    # ============================================================
    op.add_column(
        "usage_records",
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="请求耗时（毫秒）",
        ),
    )
    op.add_column(
        "usage_records",
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="是否成功",
        ),
    )
    op.add_column(
        "usage_records",
        sa.Column(
            "request_id",
            sa.String(length=100),
            nullable=True,
            comment="请求追踪 ID",
        ),
    )

    # ============================================================
    # P1-6: subscriptions 新增字段
    # ============================================================
    op.add_column(
        "subscriptions",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'active'"),
            comment="状态: active/cancelled/expired/past_due",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "billing_cycle",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'monthly'"),
            comment="计费周期: monthly/yearly",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "seats",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="席位数（用户数）",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "cancelled_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="取消时间",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "auto_renew",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="是否自动续费",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "metadata_",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="订阅元数据（支付方式、优惠券等）",
        ),
    )


def downgrade() -> None:
    """P0-P2 统一回滚。"""

    # P1-6: subscriptions 回滚
    op.drop_column("subscriptions", "metadata_")
    op.drop_column("subscriptions", "auto_renew")
    op.drop_column("subscriptions", "cancelled_at")
    op.drop_column("subscriptions", "seats")
    op.drop_column("subscriptions", "billing_cycle")
    op.drop_column("subscriptions", "status")

    # P1-5: usage_records 回滚
    op.drop_column("usage_records", "request_id")
    op.drop_column("usage_records", "success")
    op.drop_column("usage_records", "duration_ms")

    # P1-4: 10 个表 tenant_id 回滚（逆序）
    _add_tenant_id_columns = [
        ("notifications", "通知"),
        ("document_comments", "文档评论"),
        ("feedbacks", "用户反馈"),
        ("search_logs", "搜索日志"),
        ("document_actions", "文档行动项"),
        ("messages", "消息"),
        ("conversations", "对话"),
        ("graphiti_events", "实体事件"),
        ("graphiti_entities", "知识实体"),
        ("memory_facts", "记忆事实"),
    ]
    for table_name, _desc in _add_tenant_id_columns:
        op.drop_column(table_name, "tenant_id")

    # P1-3: notifications.read_at 回滚为 String(30)
    op.alter_column(
        "notifications",
        "read_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.String(length=30),
        existing_nullable=True,
        comment="已读时间 ISO 格式",
    )

    # P0-3: 删除 agent_checkpoints 表
    op.drop_table("agent_checkpoints")

    # P0-1: knowledge_bases / documents 回滚 tenant_id
    op.drop_column("documents", "tenant_id")
    op.drop_column("knowledge_bases", "tenant_id")
