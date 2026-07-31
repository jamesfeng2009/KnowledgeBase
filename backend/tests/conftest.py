"""pytest 全局配置与 fixtures。

提供测试基础设施：
- 事件循环（session 级）；
- PostgreSQL 数据库会话（自动建表）；
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
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://ekb:ekb@localhost:15432/ekb")
os.environ.setdefault("REDIS_URL", "redis://localhost:16379/15")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
# dummy API key — 避免 Provider 构造时报错（实际调用在测试中 mock）
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")


@pytest.fixture
def event_loop():
    """创建函数级事件循环 — asyncpg 连接绑定到事件循环，必须每个测试独立。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    """创建测试用数据库会话（PostgreSQL）。

    自动创建所有表，测试结束后销毁引擎，保证测试隔离。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.models.base import Base

    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        # pgvector 扩展 — memory_facts.embedding_vec 的 VECTOR 类型依赖
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # 先 drop 再 create — 确保表结构与当前 model 定义一致
        # （PostgreSQL 的 create_all 不会 ALTER 已存在的表）
        await conn.run_sync(Base.metadata.drop_all)
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
