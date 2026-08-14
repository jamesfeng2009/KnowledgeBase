"""external_credentials 表迁移安全测试。

验证：
1. external_credentials 表定义包含 tenant_id 外键到 tenants.id。
2. 建表后启用 ENABLE/FORCE ROW LEVEL SECURITY。
3. 创建 tenant_isolation 策略。
4. downgrade 正确删除策略并禁用 RLS。
"""
from __future__ import annotations

from pathlib import Path

import pytest


class TestExternalCredentialsMigration:
    """验证 external_credentials 表具备 RLS 与 FK。"""

    @pytest.fixture
    def migration_content(self) -> str:
        backend = Path(__file__).resolve().parent.parent
        migration_path = (
            backend
            / "alembic"
            / "versions"
            / "2026_08_12_1000-df4e5f608192_add_external_source_sync.py"
        )
        assert migration_path.exists(), "external_credentials 迁移文件应存在"
        return migration_path.read_text()

    def test_tenant_id_has_foreign_key(self, migration_content: str) -> None:
        assert "sa.ForeignKey('tenants.id')" in migration_content

    def test_enable_row_level_security(self, migration_content: str) -> None:
        assert 'ALTER TABLE "external_credentials" ENABLE ROW LEVEL SECURITY' in migration_content
        assert 'ALTER TABLE "external_credentials" FORCE ROW LEVEL SECURITY' in migration_content

    def test_creates_tenant_isolation_policy(self, migration_content: str) -> None:
        assert 'CREATE POLICY tenant_isolation ON "external_credentials"' in migration_content
        assert "current_setting('app.tenant_id', true)" in migration_content

    def test_downgrade_drops_policy_and_disables_rls(self, migration_content: str) -> None:
        assert 'DROP POLICY IF EXISTS tenant_isolation ON "external_credentials"' in migration_content
        assert 'ALTER TABLE "external_credentials" NO FORCE ROW LEVEL SECURITY' in migration_content
        assert 'ALTER TABLE "external_credentials" DISABLE ROW LEVEL SECURITY' in migration_content
