"""enable rls on post_rls_tables

Revision ID: d0652e7caf21
Revises: fa7b8c9d0e1f
Create Date: 2026-08-14 19:14:49.652305

为 RLS 迁移之后新增的 5 张含 tenant_id 的表启用行级安全：
- high_risk_audit_records
- tool_audit_log（先补 tenant_id 列）
- user_behaviors
- finetune_dataset_exports
- knowledge_approvals

同时补齐 tenant_id 外键指向 tenants.id，并在 RLS 策略中使用文本比较，
避免非法 UUID 导致 current_setting(...)::uuid 抛错。
"""
from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0652e7caf21"
down_revision: Union[str, Sequence[str], None] = "fa7b8c9d0e1f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 需要补 RLS 的表（按依赖顺序，被依赖的表放前面）
RLS_TABLES = [
    "high_risk_audit_records",
    "tool_audit_log",
    "user_behaviors",
    "finetune_dataset_exports",
    "knowledge_approvals",
]

# 需要补齐的外键：(表, 列, 引用表, 引用列, 约束名)
FOREIGN_KEYS = [
    (
        "high_risk_audit_records",
        "tenant_id",
        "tenants",
        "id",
        "fk_high_risk_audit_records_tenant_id_tenants",
    ),
    (
        "high_risk_audit_records",
        "user_id",
        "users",
        "id",
        "fk_high_risk_audit_records_user_id_users",
    ),
    (
        "tool_audit_log",
        "tenant_id",
        "tenants",
        "id",
        "fk_tool_audit_log_tenant_id_tenants",
    ),
    (
        "user_behaviors",
        "tenant_id",
        "tenants",
        "id",
        "fk_user_behaviors_tenant_id_tenants",
    ),
    (
        "finetune_dataset_exports",
        "tenant_id",
        "tenants",
        "id",
        "fk_finetune_dataset_exports_tenant_id_tenants",
    ),
    (
        "knowledge_approvals",
        "tenant_id",
        "tenants",
        "id",
        "fk_knowledge_approvals_tenant_id_tenants",
    ),
]


def _create_tenant_isolation_policy(table: str) -> None:
    """创建租户隔离策略，使用文本比较避免非法 UUID 抛错。"""
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
    # tool_audit_log 创建时未包含 tenant_id，先补列
    op.execute("""
        ALTER TABLE tool_audit_log
        ADD COLUMN IF NOT EXISTS tenant_id UUID;
    """)

    # 补齐外键，确保 tenant_id 参照完整性
    for table, column, ref_table, ref_column, constraint_name in FOREIGN_KEYS:
        op.execute(f"""
            ALTER TABLE "{table}"
            ADD CONSTRAINT {constraint_name}
            FOREIGN KEY ({column}) REFERENCES "{ref_table}"({ref_column});
        """)

    # 启用并强制 RLS，创建租户隔离策略
    for table in RLS_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;')
        _create_tenant_isolation_policy(table)


def downgrade() -> None:
    """Downgrade schema."""
    # 移除策略并禁用 RLS（按依赖反序）
    for table in reversed(RLS_TABLES):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;')

    # 删除外键
    for table, _, _, _, constraint_name in reversed(FOREIGN_KEYS):
        op.execute(f"""
            ALTER TABLE "{table}"
            DROP CONSTRAINT IF EXISTS {constraint_name};
        """)

    # 移除 tool_audit_log 的 tenant_id 列
    op.execute("""
        ALTER TABLE tool_audit_log
        DROP COLUMN IF EXISTS tenant_id;
    """)
