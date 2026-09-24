"""external_credentials 表租户隔离迁移测试。

验证：
1. tenant_id 有指向 tenants.id 的外键。
2. 启用 ENABLE/FORCE ROW LEVEL SECURITY 并创建 tenant_isolation 策略。
3. downgrade 正确删除策略并禁用 RLS。

这些约束不在建表迁移 df4e5f608192 里 —— 它已经跑过，改写历史不会让已存在
的库变安全，所以按仓库既有做法（参考 d0652e7caf21）由后续 revision
b8d2a0f3c5e7 补齐。测试因此校验「两张迁移合起来达成的最终 schema」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CREATE_REVISION = "2026_08_12_1000-df4e5f608192_add_external_source_sync.py"
CREATE_REV_ID = "df4e5f608192"
RLS_REVISION = "2026_09_23_1100-b8d2a0f3c5e7_add_external_credentials_rls.py"


class TestExternalCredentialsMigration:
    """验证 external_credentials 表具备 RLS 与 FK。"""

    @pytest.fixture
    def migration_content(self) -> str:
        versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
        parts = []
        for name in (CREATE_REVISION, RLS_REVISION):
            path = versions / name
            assert path.exists(), f"迁移文件 {name} 应存在"
            parts.append(path.read_text())
        return "\n".join(parts)

    def test_rls_revision_is_attached_to_history(self) -> None:
        """补 RLS 的 revision 必须真正挂在迁移链上，否则历史库跑不到它。

        直接走 alembic 的 revision map：重复 revision id 会让迁移图解析失败
        （alembic upgrade 直接跑不起来），只正则扫文件是看不出来的。
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend = Path(__file__).resolve().parent.parent
        cfg = Config(str(backend / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend / "alembic"))
        script = ScriptDirectory.from_config(cfg)

        rev_match = re.search(
            r"^revision: str = ['\"]([^'\"]+)",
            (backend / "alembic" / "versions" / RLS_REVISION).read_text(),
            re.M,
        )
        assert rev_match
        rls_rev = rev_match.group(1)

        revisions = {r.revision for r in script.walk_revisions()}
        assert rls_rev in revisions, "补 RLS 的 revision 未被 alembic 识别"
        assert len(script.get_heads()) == 1, "迁移链必须只有一个 head"
        ancestors = {r.revision for r in script.iterate_revisions(rls_rev, "base")}
        assert CREATE_REV_ID in ancestors, "补 RLS 的 revision 不在建表 revision 之后"

    def test_tenant_id_has_foreign_key(self, migration_content: str) -> None:
        assert 'FOREIGN KEY (tenant_id) REFERENCES "tenants"(id)' in migration_content

    def test_orm_model_declares_tenant_foreign_key(self) -> None:
        """ORM 也要声明，否则 create_all 建的库与迁移库不一致。"""
        from app.models.knowledge import ExternalCredential

        fks = list(ExternalCredential.__table__.c.tenant_id.foreign_keys)
        assert [str(fk.target_fullname) for fk in fks] == ["tenants.id"]

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
