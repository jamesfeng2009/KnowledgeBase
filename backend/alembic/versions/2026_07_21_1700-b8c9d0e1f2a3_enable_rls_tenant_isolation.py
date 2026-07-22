"""enable postgresql row level security for tenant isolation

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-21 17:00:00

PostgreSQL 行级安全（RLS）策略 — L4 防御层。

作为应用层隔离（L1 中间件 → L2 DI → L3 Repository）的兜底，
即使应用层出现 bug 导致跨租户查询，数据库也会拒绝返回其他租户的数据。

策略设计：
- ENABLE ROW LEVEL SECURITY：开启 RLS
- FORCE ROW LEVEL SECURITY：强制对表 Owner 也生效（仅超级用户可绕过）
- Policy tenant_isolation FOR ALL：
  - USING：当 app.tenant_id 会话变量已设置时，仅允许访问同租户数据；
           未设置时（系统操作/迁移），允许访问全部数据。
  - WITH CHECK：INSERT/UPDATE 时校验新数据的 tenant_id 必须匹配会话变量。

注意：
- 应用层通过 get_db_session 执行 SET LOCAL app.tenant_id = :tid 设置会话变量。
- 超级用户（BYPASSRLS 角色）可绕过 RLS，用于系统运维场景。
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 所有拥有 tenant_id 列的表（29 个）
TENANT_SCOPED_TABLES = [
    # P2.1 新增 tenant_id 列的表
    "users",
    "qa_questions",
    "qa_answers",
    # P2.1 补 FK 的 17 个裸字段表
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
    # 4 个 testing 表
    "test_projects",
    "test_requirements",
    "test_cases",
    "test_plans",
    # 5 个有 FK 无索引的表
    "agent_configs",
    "api_keys",
    "knowledge_gaps",
    "subscriptions",
    "usage_records",
]


def upgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        # 1. 启用 RLS
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;')

        # 2. 强制 RLS（表 Owner 也受策略约束）
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;')

        # 3. 创建租户隔离策略
        #    USING：查询/更新/删除时，仅允许访问当前租户的数据
        #    WITH CHECK：插入/更新时，校验新数据的 tenant_id 匹配当前租户
        #    当 app.tenant_id 未设置（NULL）时，允许访问全部数据（系统操作模式）
        op.execute(f"""
            CREATE POLICY tenant_isolation ON "{table}"
            FOR ALL
            USING (
                current_setting('app.tenant_id', true) IS NULL
                OR "{table}".tenant_id = current_setting('app.tenant_id', true)::uuid
            )
            WITH CHECK (
                current_setting('app.tenant_id', true) IS NULL
                OR "{table}".tenant_id = current_setting('app.tenant_id', true)::uuid
            );
        """)


def downgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        # 1. 删除策略
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";')

        # 2. 取消强制 RLS
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;')

        # 3. 禁用 RLS
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;')
