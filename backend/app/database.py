"""
数据库连接管理 — 单一职责：管理数据库引擎和会话。

遵循依赖倒置：通过 get_db_session 依赖注入，业务层不直接创建连接。
遵循开闭原则：新增数据源只需添加新的 engine 和 session 工厂。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

settings = get_settings()

# 异步引擎 — 全局共享连接池
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    echo=settings.DEBUG,
    pool_pre_ping=True,  # 连接前检查可用性，防止 "connection already closed"
)

# 异步会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # 提交后不过期，避免 lazy-loading 问题
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """依赖注入：提供数据库会话。

    使用方式：
        async def endpoint(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
