"""pytest 全局配置与 fixtures。

提供测试基础设施：
- 事件循环（session 级）；
- 内存 SQLite 数据库会话（自动建表，含 PG 类型兼容 shim）；
- 模拟已认证用户。
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

# 将 backend 目录加入 sys.path，使 ``import app`` 可用
backend_root = Path(__file__).parent.parent
sys.path.insert(0, str(backend_root))

# 测试环境变量 — 在导入 app 模块前设置
os.environ.setdefault("DEPLOY_MODE", "saas")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")


# ------------------------------------------------------------------
# PostgreSQL 类型 → SQLite 兼容 shim
# ORM 模型使用了 JSONB / ARRAY（PostgreSQL 专有类型），
# 在 SQLite 内存库建表时需编译为 SQLite 可识别的类型。
# ------------------------------------------------------------------
from sqlalchemy.dialects.postgresql import ARRAY, JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    """将 JSONB 编译为 SQLite 的 JSON 类型。"""
    return compiler.visit_JSON(element, **kw)


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    """将 ARRAY 编译为 SQLite 的 JSON 类型（以 JSON 数组存储）。"""
    return compiler.visit_JSON(element, **kw)


@pytest.fixture(scope="session")
def event_loop():
    """创建 session 级事件循环。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    """创建测试用数据库会话（内存 SQLite）。

    自动创建所有表（通过 PG→SQLite 类型 shim 兼容 JSONB/ARRAY），
    测试结束后销毁引擎，保证测试隔离。
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.models.base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def mock_user():
    """模拟已认证用户（admin 角色，internal 密级）。"""
    from app.models.user import User

    return User(
        id=uuid4(),
        email="test@ekb.com",
        hashed_password="$2b$12$testhashplaceholderfor testing only",
        name="测试用户",
        role="admin",
        clearance_level="internal",
        is_active=True,
    )
