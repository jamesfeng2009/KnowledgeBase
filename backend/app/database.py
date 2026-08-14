"""
数据库连接管理 — 单一职责：管理数据库引擎和会话。

遵循依赖倒置：通过 get_db_session 依赖注入，业务层不直接创建连接。
遵循开闭原则：新增数据源只需添加新的 engine 和 session 工厂。
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

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


# ------------------------------------------------------------------
# Celery 任务专用 — 任务级独立引擎（跨事件循环安全）
# ------------------------------------------------------------------
# 背景：全局 engine 的连接池缓存 asyncpg 连接，而连接绑定在创建它的
# 事件循环上。Celery 任务通过 asyncio.run() 为每个任务新建事件循环，
# 第 2 个任务起复用到上一个循环的连接会抛出
# "attached to a different loop"，导致 worker 崩溃。
# 因此 Celery 任务必须使用任务级（事件循环级）独立引擎：
# NullPool 不缓存连接，连接随会话关闭即释放，绝不跨循环复用；
# 上下文退出时 dispose 引擎，彻底释放资源。


def create_task_engine() -> AsyncEngine:
    """为 Celery 任务创建独立的异步引擎（NullPool，不缓存连接）。

    每次调用返回一个全新的引擎实例，供单个 Celery 任务（单个事件循环）
    独占使用；NullPool 保证连接不会被缓存并跨事件循环复用。

    Returns:
        绑定 settings.DATABASE_URL 的 NullPool AsyncEngine。
    """
    return create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        echo=settings.DEBUG,
    )


@asynccontextmanager
async def task_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Celery 任务专用数据库会话 — 任务级独立引擎，用完即 dispose。

    统一工具函数：Celery 任务的 async 代码中替代全局
    ``async_session_factory``，避免跨事件循环复用连接。

    使用方式::

        from app.database import task_db_session

        async with task_db_session() as session:
            ...
    """
    task_engine = create_task_engine()
    session_factory = async_sessionmaker(
        task_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as session:
            yield session
    finally:
        # NullPool 下连接已随会话关闭，dispose 做最终清理，安全且幂等
        await task_engine.dispose()


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """依赖注入：提供数据库会话。

    多租户隔离（P0）：
    - 从 request.state.tenant_id 获取租户 ID（由 TenantContextMiddleware 注入）
    - 执行 ``SET LOCAL app.tenant_id = xxx``，供 PostgreSQL RLS 策略使用
    - 无 tenant_id 时跳过（单租户兜底场景）

    使用方式：
        async def endpoint(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with async_session_factory() as session:
        # 注入租户上下文到 PostgreSQL session（供 RLS 策略使用）
        # 注意：asyncpg 不支持 SET LOCAL 的参数化绑定（$1 语法在 SET 中无效），
        # tenant_id 已由中间件从 JWT 解析为 UUID，可安全拼接。
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is not None:
            # 强制校验为 UUID，防止 f-string 拼接导致 SQL 注入。
            # tenant_id 正常应由 TenantContextMiddleware 解析为 uuid.UUID，
            # 此处保留防御性校验，避免任何绕过中间件的路径被利用。
            try:
                tenant_id = uuid.UUID(str(tenant_id))
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Invalid tenant_id format: {tenant_id!r}"
                ) from exc
            await session.execute(
                text(f"SET LOCAL app.tenant_id = '{tenant_id}'")
            )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
