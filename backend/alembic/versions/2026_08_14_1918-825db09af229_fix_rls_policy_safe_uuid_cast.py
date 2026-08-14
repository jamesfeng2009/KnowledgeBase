"""fix rls policy safe uuid cast

Revision ID: 825db09af229
Revises: d0652e7caf21
Create Date: 2026-08-14 19:18:31.698309

原 RLS 策略使用 current_setting('app.tenant_id', true)::uuid 做比较，
当会话变量为空字符串或非法 UUID 时会直接抛 PostgreSQL 异常，导致请求 500。

本迁移将 tenant_isolation 策略替换为 tenant_id::text 与 current_setting 的
文本比较，非法值仅会导致策略不匹配（返回空结果），不再抛错。
"""
from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "825db09af229"
down_revision: Union[str, Sequence[str], None] = "d0652e7caf21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 已在 2026_07_21_1700 启用 RLS 的 29 张表
_LEGACY_RLS_TABLES = [
    "users",
    "qa_questions",
    "qa_answers",
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
    "test_projects",
    "test_requirements",
    "test_cases",
    "test_plans",
    "agent_configs",
    "api_keys",
    "knowledge_gaps",
    "subscriptions",
    "usage_records",
    "audit_flows",
]

# 2026_08_14_1914 新启用 RLS 的 5 张表
_NEW_RLS_TABLES = [
    "high_risk_audit_records",
    "tool_audit_log",
    "user_behaviors",
    "finetune_dataset_exports",
    "knowledge_approvals",
]

RLS_TABLES = _LEGACY_RLS_TABLES + _NEW_RLS_TABLES


def _create_safe_policy(table: str) -> None:
    """创建使用文本比较的安全租户隔离策略。"""
    op.execute(f"""
        CREATE POLICY tenant_isolation ON "{table}"
        FOR ALL
        USING (
            current_setting('app.tenant_id', true) IS NULL
            OR lower("{table}".tenant_id::text) = lower(current_setting('app.tenant_id', true))
        )
        WITH CHECK (
            current_setting('app.tenant_id', true) IS NULL
            OR lower("{table}".tenant_id::text) = lower(current_setting('app.tenant_id', true))
        );
    """)


def upgrade() -> None:
    """Upgrade schema."""
    for table in RLS_TABLES:
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";')
        _create_safe_policy(table)


def downgrade() -> None:
    """Downgrade schema — 恢复为原来的 ::uuid 比较策略。"""
    for table in RLS_TABLES:
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";')
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
