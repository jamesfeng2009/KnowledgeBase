"""pytest 全局配置与 fixtures。

提供测试基础设施：
- 事件循环（function 级，asyncpg 连接绑定到 loop 必须每测试独立）；
- PostgreSQL 数据库会话（事务回滚隔离 SAVEPOINT）；
- 模拟已认证用户。

DB 隔离机制演进（见 technical-qa.md 11.1）：
- 旧方案：function 级 DROP/CREATE ALL，DROP TABLE 要 AccessExclusiveLock，
  并发跑多 pytest 进程时 AB-BA 死锁；
- 新方案：模块级初始化表结构一次 + function 级 savepoint 回滚，无 DROP 无死锁。
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
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://ekb:ekb@localhost:15432/ekb")
os.environ.setdefault("REDIS_URL", "redis://localhost:16379/15")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
# dummy API key — 避免 Provider 构造时报错（实际调用在测试中 mock）
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")


def _init_test_schema() -> None:
    """模块导入时同步初始化测试库表结构（一次性 DROP+CREATE）。

    用 asyncio.run() 在临时 loop 上执行，避免 async fixture 的 loop 绑定问题
    （asyncpg 连接绑定到创建它的 event_loop，session scope fixture 的 loop 与
    测试 loop 不一致时报 "attached to a different loop"）。
    后续测试只做 create_all（IF NOT EXISTS，极快）+ savepoint 回滚，无 DROP 无死锁。
    DB 不可用时静默跳过（纯 mock 测试不需要 DB）。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models.base import Base

    async def _init() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_init())
    except Exception:
        pass  # DB 不可用时静默（mock 测试不需要 DB）


_init_test_schema()


@pytest.fixture
def event_loop():
    """function 级事件循环 — asyncpg 连接绑定到 loop，每个测试独立。

    保持 function scope：session scope event_loop 会导致 asyncpg 连接池跨 loop
    （pytest-asyncio 的 loop 管理与 session scope async fixture 不完全兼容，
    即使 loop_scope="session" 仍报 "attached to a different loop"）。
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    """function 级会话 — 事务回滚隔离（SAVEPOINT），测试结束回滚，数据不残留。

    替代原 DROP/CREATE ALL 隔离方案：
    - 原方案每个测试 DROP+CREATE 全部表，DROP TABLE 要 AccessExclusiveLock，
      并发跑多 pytest 进程时经典 AB-BA 死锁；
    - 新方案表结构由模块级 _init_test_schema() 保证，每个测试只 create_all
      （IF NOT EXISTS，极快）+ savepoint 回滚，无 DROP 无死锁。
    NullPool：不缓存连接，每次 connect() 新建绑定当前 loop（asyncpg 安全）。
    join_transaction_mode="create_savepoint"：session 在外层事务内用 savepoint
    嵌套，session.commit() 只 release savepoint 不会真正提交，结束时外层
    rollback 撤销全部更改。
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models.base import Base

    engine = create_async_engine(
        os.environ["DATABASE_URL"], echo=False, poolclass=NullPool
    )
    # create_all IF NOT EXISTS — 表已存在则跳过（保证新增模型表及时创建）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
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


@pytest.fixture(autouse=True)
def clear_lru_caches():
    """每个测试前清理 lru_cache — 防止跨测试的缓存污染。

    get_vision_provider / get_settings 等函数使用 @lru_cache，
    如果某个测试未 mock 就调用了真实实现，缓存会残留到后续测试。
    """
    try:
        from app.vlm.provider import get_vision_provider
        get_vision_provider.cache_clear()
    except Exception:
        pass
    try:
        from app.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass
    yield
