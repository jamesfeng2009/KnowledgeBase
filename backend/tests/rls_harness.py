"""真实 RLS 集成测试基座（C1）— 让验收打到生产边界，而不是 create_all 的影子。

conftest 的 ``db_session`` 用 ``Base.metadata.create_all`` 建表：没有策略、
连接角色是 superuser —— PG 对 superuser 无条件绕过 RLS，所以「租户隔离」
类断言在那种基座上恒绿。本基座把生产的三件套原样搭起来：

1. **表** — ``create_all``（与 conftest 同源，表结构来自生产 ORM）；
2. **策略** — 直接执行生产迁移 ``e8f9a0b1c2d3`` 的 fail-closed SQL
   （从迁移模块加载，不手写副本 —— 策略文本改了这里立刻跟着变）；
3. **角色** — ``scripts/provision_app_role.provision`` 建非 superuser /
   NOBYPASSRLS 的最小权限角色，业务会话全部走该角色连接。

用法::

    async def test_x(rls_db):
        async with rls_db.factory() as session:
            async with tenant_scope(session, TENANT_A):
                ...  # 生产 tenant_context API 设 GUC

数据库不可达 / 当前连接无建角色权限时整组跳过（与 test_rls_role_split 一致）。
"""
from __future__ import annotations

import importlib.util
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio

BACKEND_DIR = Path(__file__).resolve().parents[1]
RLS_MIGRATION_FILE = (
    BACKEND_DIR / "alembic" / "versions"
    / "2026_11_10_1800-e8f9a0b1c2d3_rls_fail_closed.py"
)
PROVISION_SCRIPT = BACKEND_DIR / "scripts" / "provision_app_role.py"

#: 独立测试角色 —— 不复用 ekb_app / ekb_app_pytest，避免与其它套件的
#: provision/清理互相改口令。
RLS_TEST_ROLE = "ekb_rls_mem_pytest"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owner_dsn() -> str:
    """owner 连接串（asyncpg 直连格式）。"""
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_credentials(dsn: str, role: str, password: str) -> str:
    parts = urlsplit(dsn)
    netloc = f"{role}:{password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest_asyncio.fixture(scope="module")
async def rls_db():
    """yield SimpleNamespace(factory=运行角色会话工厂, owner_url=owner 连接串)。

    模块级共享：建表/建策略/建角色只做一次；用例间的数据清理由使用方
    自行 TRUNCATE（owner 身份）。
    """
    asyncpg = pytest.importorskip("asyncpg")
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import app.models  # noqa: F401 — 触发全部模型注册
    from app.models.base import Base

    owner_url = os.environ["DATABASE_URL"]
    owner_engine = create_async_engine(owner_url)
    app_engine = None
    role_ready = False
    try:
        try:
            async with owner_engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        except Exception as exc:  # noqa: BLE001 — 数据库不可用时跳过而不是误报
            pytest.skip(f"无法准备测试库：{type(exc).__name__}: {exc}")

        # 策略来自生产迁移本体（fail-closed），不是手写的近似副本
        mig = _load_module("rls_fail_closed_for_harness", RLS_MIGRATION_FILE)
        async with owner_engine.begin() as conn:
            await conn.execute(
                sa.text(mig._TENANT_TABLES_LOOP.format(predicate=mig._STRICT_PREDICATE))
            )

        prov = _load_module("provision_for_rls_harness", PROVISION_SCRIPT)
        password = f"pytest_{secrets.token_hex(8)}"
        try:
            await prov.provision(owner_dsn(), RLS_TEST_ROLE, password)
        except PermissionError as exc:
            pytest.skip(f"当前连接角色无建角色权限：{exc}")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"无法准备运行角色：{type(exc).__name__}: {exc}")
        role_ready = True

        app_dsn = _with_credentials(owner_dsn(), RLS_TEST_ROLE, password)
        app_url = app_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        # NullPool：每会话一条新连接，GUC 不会串到下一个用例
        app_engine = create_async_engine(app_url, poolclass=NullPool)
        factory = sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)
        yield SimpleNamespace(factory=factory, owner_url=owner_dsn())
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await owner_engine.dispose()
        if role_ready:
            try:
                conn = await asyncpg.connect(owner_dsn())
                try:
                    await conn.execute(f'DROP OWNED BY "{RLS_TEST_ROLE}"')
                    await conn.execute(f'DROP ROLE IF EXISTS "{RLS_TEST_ROLE}"')
                finally:
                    await conn.close()
            except Exception:  # noqa: BLE001 — 清理是 best-effort
                pass