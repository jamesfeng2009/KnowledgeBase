"""add tenant_id to users and qa tables, add FK and indexes

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-21 16:00:00

多租户数据隔离 P2 迁移：
1. users 表新增 tenant_id 列（FK → tenants.id + 索引）
2. qa_questions / qa_answers 表新增 tenant_id 列（FK + 索引）
3. 21 个已有 tenant_id 列但无 FK 的表补齐外键约束
4. 22 个已有 tenant_id 列但无索引的表补齐 B-tree 索引
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── 需要新增 tenant_id 列的表（本迁移新建列） ──
NEW_COLUMN_TABLES = [
    "users",
    "qa_questions",
    "qa_answers",
]

# ── 已有 tenant_id 列但缺少 FK 约束的表（补 FK） ──
# 17 个裸字段表 + 4 个 testing 表（有索引无 FK）= 21 个
TABLES_NEED_FK = [
    # 17 个裸字段表（b2c3d4e5f6a7 迁移批量加列，无 FK 无索引）
    "knowledge_bases",
    "documents",
    "memory_facts",
    "graphiti_entities",
    "graphiti_events",
    "conversations",
    "messages",
    "document_actions",
    "search_logs",
    "feedbacks",
    "document_comments",
    "notifications",
    "tool_approvals",
    "user_model_preferences",
    "knowledge_assets",
    "compounding_tasks",
    "knowledge_conflicts",
    # 4 个 testing 表（有索引无 FK）
    "test_projects",
    "test_requirements",
    "test_cases",
    "test_plans",
]

# ── 已有 tenant_id 列但缺少索引的表（补 B-tree 索引） ──
# 17 个裸字段表 + 5 个有 FK 无索引的表 = 22 个
# 注意：4 个 testing 表已有索引，不重复创建
TABLES_NEED_INDEX = [
    # 17 个裸字段表
    "knowledge_bases",
    "documents",
    "memory_facts",
    "graphiti_entities",
    "graphiti_events",
    "conversations",
    "messages",
    "document_actions",
    "search_logs",
    "feedbacks",
    "document_comments",
    "notifications",
    "tool_approvals",
    "user_model_preferences",
    "knowledge_assets",
    "compounding_tasks",
    "knowledge_conflicts",
    # 5 个有 FK 无索引的表
    "agent_configs",
    "api_keys",
    "knowledge_gaps",
    "subscriptions",
    "usage_records",
]


def upgrade() -> None:
    # ── 1. 新增 tenant_id 列（users + qa_questions + qa_answers） ──
    for table in NEW_COLUMN_TABLES:
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                sa.UUID(as_uuid=True),
                nullable=True,
                comment="租户 ID",
            ),
        )

    # ── 2. 为所有需要 FK 的表创建外键约束 ──
    # 新增列的 3 个表 + 已有列但无 FK 的 21 个表 = 24 个
    all_fk_tables = NEW_COLUMN_TABLES + TABLES_NEED_FK
    for table in all_fk_tables:
        op.create_foreign_key(
            f"fk_{table}_tenant_id",
            table,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # ── 3. 为所有需要索引的表创建 B-tree 索引 ──
    # 新增列的 3 个表 + 已有列但无索引的 22 个表 = 25 个
    all_index_tables = NEW_COLUMN_TABLES + TABLES_NEED_INDEX
    for table in all_index_tables:
        op.create_index(
            f"ix_{table}_tenant_id",
            table,
            ["tenant_id"],
        )


def downgrade() -> None:
    # ── 3. 删除索引 ──
    all_index_tables = NEW_COLUMN_TABLES + TABLES_NEED_INDEX
    for table in all_index_tables:
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)

    # ── 2. 删除外键约束 ──
    all_fk_tables = NEW_COLUMN_TABLES + TABLES_NEED_FK
    for table in all_fk_tables:
        op.drop_constraint(f"fk_{table}_tenant_id", table, type_="foreignkey")

    # ── 1. 删除 tenant_id 列 ──
    for table in NEW_COLUMN_TABLES:
        op.drop_column(table, "tenant_id")
