"""add tenant_id to audit_flows + file_size to documents + rls for audit_flows

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-21 18:00:00

三合一迁移：
1. MT-2: audit_flows 表加 tenant_id 列 + 索引
2. MT-3: audit_flows 表启用 RLS 策略（与 29 表保持一致）
3. BUG-3: documents 表加 file_size 列（修复 _should_use_multipart_pipeline
   访问不存在的 doc.file_size 属性导致 GB 视频分流静默失效）
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # === MT-2: audit_flows 加 tenant_id 列 + FK + 索引 ===
    op.add_column(
        "audit_flows",
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_audit_flows_tenant_id",
        "audit_flows",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_audit_flows_tenant_id", "audit_flows", ["tenant_id"])

    # === BUG-3: documents 加 file_size 列 ===
    op.add_column(
        "documents",
        sa.Column("file_size", sa.BigInteger(), nullable=True),
    )

    # === MT-3: audit_flows 启用 RLS 策略 ===
    op.execute('ALTER TABLE "audit_flows" ENABLE ROW LEVEL SECURITY;')
    op.execute('ALTER TABLE "audit_flows" FORCE ROW LEVEL SECURITY;')
    op.execute("""
        CREATE POLICY tenant_isolation ON "audit_flows"
        FOR ALL
        USING (
            current_setting('app.tenant_id', true) IS NULL
            OR "audit_flows".tenant_id = current_setting('app.tenant_id', true)::uuid
        )
        WITH CHECK (
            current_setting('app.tenant_id', true) IS NULL
            OR "audit_flows".tenant_id = current_setting('app.tenant_id', true)::uuid
        );
    """)


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "audit_flows";')
    op.execute('ALTER TABLE "audit_flows" NO FORCE ROW LEVEL SECURITY;')
    op.execute('ALTER TABLE "audit_flows" DISABLE ROW LEVEL SECURITY;')

    op.drop_column("documents", "file_size")
    op.drop_index("ix_audit_flows_tenant_id", table_name="audit_flows")
    op.drop_constraint("fk_audit_flows_tenant_id", "audit_flows", type_="foreignkey")
    op.drop_column("audit_flows", "tenant_id")
