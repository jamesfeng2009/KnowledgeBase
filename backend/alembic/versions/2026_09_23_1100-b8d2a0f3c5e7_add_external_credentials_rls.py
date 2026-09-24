"""add external_credentials tenant foreign key and rls

Revision ID: b8d2a0f3c5e7
Revises: a7c1f9e2b4d6
Create Date: 2026-09-23 11:00:00.000000

df4e5f608192 建 external_credentials 时只写了 tenant_id 列，既没有指向
tenants.id 的外键，也没有启用 RLS —— 而这张表存的是各租户外部平台
（飞书 / Confluence / Notion）的 AES-GCM 凭证密文，且已经通过
/api/v1/external_credentials 暴露。同批含 tenant_id 的表都在
d0652e7caf21 里补齐了同样的约束，这张表是漏网之鱼。

迁移历史不可改写，所以按仓库既有做法用一支后续 revision 补齐：
补 tenant_id 外键 + ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation
策略（沿用文本比较，避免非法 UUID 让 current_setting(...)::uuid 抛错）。
"""
from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d2a0f3c5e7"
down_revision: Union[str, Sequence[str], None] = "a7c1f9e2b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — 补 tenant_id 外键并启用租户隔离。"""
    op.execute("""
        ALTER TABLE "external_credentials"
        ADD CONSTRAINT fk_external_credentials_tenant_id_tenants
        FOREIGN KEY (tenant_id) REFERENCES "tenants"(id);
    """)
    op.execute('ALTER TABLE "external_credentials" ENABLE ROW LEVEL SECURITY;')
    op.execute('ALTER TABLE "external_credentials" FORCE ROW LEVEL SECURITY;')
    op.execute("""
        CREATE POLICY tenant_isolation ON "external_credentials"
        FOR ALL
        USING (
            current_setting('app.tenant_id', true) IS NULL
            OR lower("external_credentials".tenant_id::text) = lower(current_setting('app.tenant_id', true))
        )
        WITH CHECK (
            current_setting('app.tenant_id', true) IS NULL
            OR lower("external_credentials".tenant_id::text) = lower(current_setting('app.tenant_id', true))
        );
    """)


def downgrade() -> None:
    """Downgrade schema — 撤销策略、RLS 与外键。"""
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "external_credentials";')
    op.execute('ALTER TABLE "external_credentials" NO FORCE ROW LEVEL SECURITY;')
    op.execute('ALTER TABLE "external_credentials" DISABLE ROW LEVEL SECURITY;')
    op.execute("""
        ALTER TABLE "external_credentials"
        DROP CONSTRAINT IF EXISTS fk_external_credentials_tenant_id_tenants;
    """)
