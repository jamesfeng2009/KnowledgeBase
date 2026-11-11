"""多租户 RLS 落地门禁 — 角色拆分 + 策略覆盖 + 迁移连接分离。

这三件事是一套的，缺一件 RLS 就是摆设：

1. **运行角色必须是非 superuser**：PostgreSQL 对 superuser 无条件绕过 RLS
   （`FORCE ROW LEVEL SECURITY` 只约束表 owner，不约束 superuser）。应用历史上
   一直用建库的 superuser 连接，所以 36 张表的策略从未生效过。
2. **策略必须覆盖到每一张带 tenant_id 的表**：漏一张，那张表就是零防护
   （本次补齐前漏了 12 张，含 document_chunks）。
3. **迁移连接与运行连接必须能分离**：建表要 owner，运行要最小权限角色，
   两者权限要求相反，只能两条 URL。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import secrets
import textwrap
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio

BACKEND_DIR = Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
SCRIPTS_DIR = BACKEND_DIR / "scripts"

APP_ROLE = "ekb_app"
#: 测试专用角色名 —— 刻意不复用 APP_ROLE。
#: provision() 每次都会 ALTER ROLE ... PASSWORD，若直接拿 ekb_app 来做测试，
#: 每跑一次全量就把开发/预发上那条连接串的口令改掉，属于典型的「测试污染环境」。
TEST_ROLE = "ekb_app_pytest"
PROBE_TABLE = "_rls_role_probe"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owner_dsn() -> str:
    """owner 连接串（asyncpg 直连格式）。"""
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_credentials(dsn: str, user: str, password: str) -> str:
    parts = urlsplit(dsn)
    netloc = f"{user}:{password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ======================================================================
# 一、策略覆盖门禁（静态，不需要数据库）
# ======================================================================


def _tenant_scoped_model_tables() -> set[str]:
    """模型里所有声明了 tenant_id 列的表。"""
    import app.models  # noqa: F401  — 触发全部模型注册
    from app.models.base import Base

    return {t.name.lower() for t in Base.metadata.sorted_tables if "tenant_id" in t.columns}


def _rls_covered_tables() -> set[str]:
    """迁移里开过 RLS 的表。

    两种写法都要认：
        - 模块级列表常量（`TENANT_SCOPED_TABLES = [...]`）+ 循环 ALTER
        - 直接的 `ALTER TABLE x ENABLE ROW LEVEL SECURITY`
    只认前者会漏掉后者的表，只认正则又会漏掉列表写法 —— 两种都解析才不会误报。
    """
    covered: set[str] = set()
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "ROW LEVEL SECURITY" not in text.upper():
            continue
        for node in ast.parse(text).body:
            name = ""
            value = None
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            if value is None or "TABLE" not in name.upper():
                continue
            try:
                items = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(items, (list, tuple)):
                covered.update(
                    item.lower() for item in items if isinstance(item, str)
                )
        covered.update(
            m.group(1).lower()
            for m in re.finditer(
                r"ALTER TABLE\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s+ENABLE ROW LEVEL SECURITY",
                text,
                re.I,
            )
        )
    return covered


class TestRlsPolicyCoverage:
    def test_every_tenant_table_has_rls_migration(self) -> None:
        """带 tenant_id 的表必须都在某个迁移里开过 RLS。

        这条是最省事也最有效的防线：新加一张租户表却忘了开策略，
        在没有这条断言之前完全不会被发现（本次就漏了 12 张）。
        """
        missing = sorted(_tenant_scoped_model_tables() - _rls_covered_tables())
        assert not missing, (
            "以下表声明了 tenant_id 但没有任何迁移为它开启 RLS，"
            "数据库层对该表零防护：\n  "
            + "\n  ".join(missing)
            + "\n请在新增表的迁移里补 ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation 策略。"
        )

    def test_rls_policy_does_not_drop_force_flag(self) -> None:
        """每个开 RLS 的迁移都必须同时写 FORCE —— 否则表 owner 仍能绕过。"""
        offenders: list[str] = []
        for path in sorted(VERSIONS_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "ENABLE ROW LEVEL SECURITY" not in text.upper():
                continue
            if "FORCE ROW LEVEL SECURITY" not in text.upper():
                offenders.append(path.name)
        assert not offenders, "这些迁移只 ENABLE 没有 FORCE，表 owner 仍可绕过 RLS：\n" + "\n".join(
            offenders
        )


# ======================================================================
# 二、迁移连接分离（配置层）
# ======================================================================


class TestMigrationDatabaseUrl:
    def test_settings_falls_back_to_database_url(self) -> None:
        from app.config import Settings

        s = Settings(
            DATABASE_URL="postgresql+asyncpg://a:b@h:5432/db",
            MIGRATION_DATABASE_URL=None,
        )
        assert s.migration_database_url == s.DATABASE_URL

    def test_settings_prefers_migration_url(self) -> None:
        from app.config import Settings

        s = Settings(
            DATABASE_URL="postgresql+asyncpg://app:p@h:5432/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://owner:p@h:5432/db",
        )
        assert s.migration_database_url.endswith("//owner:p@h:5432/db")
        assert s.DATABASE_URL.endswith("//app:p@h:5432/db")

    def test_empty_migration_url_normalised_to_none(self) -> None:
        """空字符串按「未配置」处理 —— compose 里常写 `MIGRATION_DATABASE_URL=` 占位。"""
        from app.config import Settings

        s = Settings(DATABASE_URL="postgresql+asyncpg://a:b@h:5432/db", MIGRATION_DATABASE_URL="")
        assert s.MIGRATION_DATABASE_URL is None
        assert s.migration_database_url == s.DATABASE_URL

    def test_migration_url_rejects_sync_driver(self) -> None:
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                DATABASE_URL="postgresql+asyncpg://a:b@h:5432/db",
                MIGRATION_DATABASE_URL="postgresql://a:b@h:5432/db",
            )

    def test_alembic_env_uses_migration_url(self) -> None:
        """回归：env.py 不能退回 settings.DATABASE_URL，否则迁移会以低权限角色执行。"""
        text = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
        assert "settings.migration_database_url" in text

    def test_runtime_migration_runner_prefers_migration_url(self) -> None:
        """AUTO_MIGRATE 启动路径同样要走 owner 连接。"""
        text = (BACKEND_DIR / "app" / "utils" / "migration.py").read_text(encoding="utf-8")
        assert "MIGRATION_DATABASE_URL" in text
        assert text.index("MIGRATION_DATABASE_URL") < text.index('os.environ.get("DATABASE_URL"')


class TestProvisionScript:
    def test_default_role_name_is_ekb_app(self) -> None:
        """文档与脚本里约定的运行角色名是 ekb_app（测试用独立的 *_pytest 角色）。"""
        text = (SCRIPTS_DIR / "provision_app_role.py").read_text(encoding="utf-8")
        assert f'"{APP_ROLE}"' in text

    def test_script_never_grants_superuser(self) -> None:
        """回归：供给脚本只在 nosuperuser / nobypassrls 方向创建角色。"""
        text = (SCRIPTS_DIR / "provision_app_role.py").read_text(encoding="utf-8")
        assert "nosuperuser nobypassrls" in text
        assert "superuser login" not in text.lower()


# ======================================================================
# 三、运行角色与真实 RLS 行为（需要 PG）
# ======================================================================


@pytest_asyncio.fixture(scope="module")
async def app_role_credential():
    """在测试库里把运行角色建出来，返回 (role, password)。

    连不上 / 不是 superuser 时跳过 —— 这类测试只在能建角色的环境里有意义。
    用独立的 TEST_ROLE，避免改动真实运行角色 ekb_app 的口令。
    """
    asyncpg = pytest.importorskip("asyncpg")
    prov = _load("ekb_provision_app_role", SCRIPTS_DIR / "provision_app_role.py")
    password = f"pytest_{secrets.token_hex(8)}"
    try:
        await prov.provision(_owner_dsn(), TEST_ROLE, password)
    except PermissionError as exc:
        pytest.skip(f"当前连接角色无建角色权限：{exc}")
    except Exception as exc:  # noqa: BLE001 - 数据库不可用时跳过而不是误报失败
        pytest.skip(f"无法准备运行角色：{type(exc).__name__}: {exc}")
    yield TEST_ROLE, password

    # 收尾：撤权后删角色，避免在集群里留垃圾（失败不影响测试结论）
    try:
        conn = await asyncpg.connect(_owner_dsn())
        try:
            await conn.execute(f'DROP OWNED BY "{TEST_ROLE}"')
            await conn.execute(f'DROP ROLE IF EXISTS "{TEST_ROLE}"')
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001 - 清理是 best-effort
        pass


class TestAppRolePrivileges:
    @pytest.mark.asyncio
    async def test_role_is_not_superuser_and_cannot_bypass_rls(
        self, app_role_credential
    ) -> None:
        """核心断言：运行角色既不是 superuser，也没有 BYPASSRLS。

        任意一条为真，RLS 都会被无条件绕过 —— 这正是历史上隔离失效的原因。
        """
        asyncpg = pytest.importorskip("asyncpg")
        role, _ = app_role_credential
        conn = await asyncpg.connect(_owner_dsn())
        try:
            row = await conn.fetchrow(
                "select rolsuper, rolbypassrls, rolcanlogin from pg_roles where rolname = $1",
                role,
            )
        finally:
            await conn.close()
        assert row is not None, f"角色 {role} 不存在"
        assert row["rolcanlogin"] is True
        assert row["rolsuper"] is False, "运行角色是 superuser → RLS 完全失效"
        assert row["rolbypassrls"] is False, "运行角色带 BYPASSRLS → RLS 完全失效"


class TestRlsActuallyFilters:
    """用一张与业务表策略完全一致的探针表，验证「换角色后 RLS 真的拦得住」。"""

    @pytest.mark.asyncio
    async def test_tenant_isolation_between_two_tenants(self, app_role_credential) -> None:
        asyncpg = pytest.importorskip("asyncpg")
        role, password = app_role_credential
        tenant_a = "11111111-1111-1111-1111-111111111111"
        tenant_b = "22222222-2222-2222-2222-222222222222"

        owner = await asyncpg.connect(_owner_dsn())
        try:
            await owner.execute(f'DROP TABLE IF EXISTS "{PROBE_TABLE}"')
            await owner.execute(
                f'CREATE TABLE "{PROBE_TABLE}" (id int primary key, tenant_id uuid not null)'
            )
            await owner.execute(f'ALTER TABLE "{PROBE_TABLE}" ENABLE ROW LEVEL SECURITY')
            await owner.execute(f'ALTER TABLE "{PROBE_TABLE}" FORCE ROW LEVEL SECURITY')
            await owner.execute(
                f"""
                CREATE POLICY tenant_isolation ON "{PROBE_TABLE}"
                FOR ALL
                USING (
                    current_setting('app.tenant_id', true) IS NULL
                    OR "{PROBE_TABLE}".tenant_id = current_setting('app.tenant_id', true)::uuid
                )
                """
            )
            await owner.execute(
                f'INSERT INTO "{PROBE_TABLE}" VALUES (1, $1::uuid), (2, $2::uuid)',
                tenant_a,
                tenant_b,
            )
            await owner.execute(f'GRANT SELECT ON "{PROBE_TABLE}" TO "{role}"')
        finally:
            await owner.close()

        app_dsn = _with_credentials(_owner_dsn(), role, password)
        app = await asyncpg.connect(app_dsn)
        try:
            async with app.transaction():
                await app.execute("select set_config('app.tenant_id', $1, true)", tenant_a)
                rows = await app.fetch(f'SELECT id, tenant_id FROM "{PROBE_TABLE}" ORDER BY id')
            visible = {str(r["tenant_id"]) for r in rows}
            assert visible == {tenant_a}, f"角色 {role} 看到了其它租户的数据：{visible}"
        finally:
            await app.close()
            cleanup = await asyncpg.connect(_owner_dsn())
            try:
                await cleanup.execute(f'DROP TABLE IF EXISTS "{PROBE_TABLE}"')
            finally:
                await cleanup.close()

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "已知设计缺陷：策略第一条分支 current_setting('app.tenant_id', true) IS NULL "
            "是 fail-open —— GUC 未设置时全表可见。关闭它是第 8.4 节的第 2 步，"
            "会打破「单租户兜底」现状，需业务确认。真关掉后本用例会变成 XPASS(strict) 失败，"
            "届时删掉它即可。"
        ),
    )
    async def test_unset_guc_is_fail_open_today(self, app_role_credential) -> None:
        """记录当前行为：不设 GUC 时能看到全部租户的数据。

        这条不是「应该这样」，而是「现在就是这样」——把它写成可执行的断言，
        是为了让「关掉 fail-open」这个动作有明确的可观测后果，而不是一句口号。
        """
        asyncpg = pytest.importorskip("asyncpg")
        role, password = app_role_credential
        tenant_a = "11111111-1111-1111-1111-111111111111"

        owner = await asyncpg.connect(_owner_dsn())
        try:
            await owner.execute(f'DROP TABLE IF EXISTS "{PROBE_TABLE}"')
            await owner.execute(
                f'CREATE TABLE "{PROBE_TABLE}" (id int primary key, tenant_id uuid not null)'
            )
            await owner.execute(f'ALTER TABLE "{PROBE_TABLE}" ENABLE ROW LEVEL SECURITY')
            await owner.execute(f'ALTER TABLE "{PROBE_TABLE}" FORCE ROW LEVEL SECURITY')
            await owner.execute(
                f"""
                CREATE POLICY tenant_isolation ON "{PROBE_TABLE}"
                FOR ALL
                USING (
                    current_setting('app.tenant_id', true) IS NULL
                    OR "{PROBE_TABLE}".tenant_id = current_setting('app.tenant_id', true)::uuid
                )
                """
            )
            await owner.execute(
                f'INSERT INTO "{PROBE_TABLE}" VALUES (1, $1::uuid), (2, $2::uuid)',
                tenant_a,
                "22222222-2222-2222-2222-222222222222",
            )
            await owner.execute(f'GRANT SELECT ON "{PROBE_TABLE}" TO "{role}"')
        finally:
            await owner.close()

        app = await asyncpg.connect(_with_credentials(_owner_dsn(), role, password))
        try:
            count = await app.fetchval(f'SELECT count(*) FROM "{PROBE_TABLE}"')
        finally:
            await app.close()
            cleanup = await asyncpg.connect(_owner_dsn())
            try:
                await cleanup.execute(f'DROP TABLE IF EXISTS "{PROBE_TABLE}"')
            finally:
                await cleanup.close()

        # fail-open：两条租户的行都能看到 → 断言「等于 2」触发 xfail
        assert count == 1, textwrap.dedent(
            f"""
            GUC 未设置时看到 {count} 行（预期只有 1 行才叫 fail-closed）。
            说明 fail-open 分支已被关闭 —— 请删除本用例并更新
            docs/P0-multi-tenant-isolation-design.md 第 8.4 节。
            """
        )
