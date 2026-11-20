"""
数据库连接管理 — 单一职责：管理数据库引擎和会话。

遵循依赖倒置：通过 get_db_session 依赖注入，业务层不直接创建连接。
遵循开闭原则：新增数据源只需添加新的 engine 和 session 工厂。
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.tenant_context import (
    TENANT_GUC,
    apply_cross_tenant,
    apply_tenant,
    clear_tenant,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

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


@event.listens_for(engine.sync_engine, "checkin")
def _reset_tenant_guc_on_checkin(dbapi_connection, connection_record) -> None:
    """连接归还连接池前清掉租户 GUC。

    为什么必须有这一层：``app.tenant_id`` 是**连接级**状态，而主引擎是
    QueuePool —— 同一条物理连接会被不同请求复用。归还时若 GUC 还留着上一个
    请求的租户（最糟是 ``'*'``），下一个请求即使一个租户都没设，也会带着它，
    RLS 隔离当场失效。

    会话层（``get_db_session`` 的 finally）已经 clear 过一次，但那里依赖
    ``session.commit()`` 才真正落库；一旦 commit 失败连接仍会回池。这一层是
    DBAPI 级的最后兜底。

    重置失败时**丢弃连接**而不是放回池 —— 带着未知 GUC 的连接等于一颗定时炸弹。
    """
    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"RESET {TENANT_GUC}")
        finally:
            cursor.close()
    except Exception:
        log.warning("db.tenant_guc_reset_failed", exc_info=True)
        with suppress(Exception):
            connection_record.invalidate()


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
async def task_db_session(
    tenant_id: Any = None,
    *,
    cross_tenant_reason: str | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Celery 任务专用数据库会话 — 任务级独立引擎，用完即 dispose。

    统一工具函数：Celery 任务的 async 代码中替代全局
    ``async_session_factory``，避免跨事件循环复用连接。

    **必须声明身份**（RLS fail-closed 之后）：

    - ``tenant_id=...`` — 严格租户作用域，最常见
    - ``cross_tenant_reason="..."`` — 跨租户，理由必填并记 ``tenant.cross_tenant_task``
    - 都不传 — 不设 GUC，**任何租户表都查不到数据**

    第三种不是"降级运行"，是刻意的默认拒绝。它只服务一种流程：任务拿不到
    租户、需要先按主键反查归属。这种两步走必须紧接着 ``apply_tenant(session, tid)``，
    见 ``resolve_task_tenant()``。

    使用方式::

        async with task_db_session() as session:
            tid = await resolve_task_tenant(session, tenant_id=tenant_id, doc_id=doc_id)
            await apply_tenant(session, tid)     # 就地切换，无需额外缩进
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
            effective = tenant_id
            if effective is None and cross_tenant_reason is None:
                # 入队时带了 tenant_id 的任务，由 task_prerun 信号存进 ContextVar。
                # 这里捞出来，省得每个任务内部再传一层参。
                from app.core.task_context import current_task_tenant

                effective = current_task_tenant()

            if effective is not None and str(effective).strip():
                await apply_tenant(session, effective)
            elif cross_tenant_reason is not None:
                await apply_cross_tenant(session)
                log.warning(
                    "tenant.cross_tenant_task",
                    reason=cross_tenant_reason.strip() or "(empty)",
                )
            else:
                # 没有身份：不设 GUC，RLS fail-closed，租户表一律查不到。
                # 只服务「先按主键反查归属」的两步流程，见 resolve_task_tenant()。
                log.debug("task.db_session.without_tenant")
            yield session
    finally:
        # NullPool：连接随引擎 dispose 一起销毁，GUC 不可能残留到下一个任务
        await task_engine.dispose()


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """依赖注入：提供数据库会话，并把请求租户写入 RLS 的 GUC。

    多租户隔离（P0）：
    - 从 request.state.tenant_id 取租户（由 TenantContextMiddleware 注入）
    - 有租户 → 严格租户作用域，RLS 只放行该租户行
    - **无租户 → 不设 GUC，RLS fail-closed，一行都查不到**

    第三点是刻意为之。此前无租户时跳过设置，而策略是 fail-open，于是
    "没有租户上下文"等于"看得到全部租户"。现在它等于"什么都看不到"——
    哪个端点需要跨租户，就必须显式依赖 ``get_system_db_session``，
    这样每一处越权访问在代码里都可见。

    注：GUC 用 ``set_config(..., false)``（session 级）而非 ``SET LOCAL``。
    后者随 commit 失效，业务代码只要 commit 过一次，后续查询在 fail-closed
    下就会静默返回空集 —— 表现为"偶尔查不到数据"，极难定位。

    使用方式：
        async def endpoint(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with async_session_factory() as session:
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is not None:
            # normalize_tenant_id 做了 UUID 强校验，既防拼接注入，
            # 也避免非法值让 GUC 变成一个永远匹配不上的字符串。
            await apply_tenant(session, tenant_id)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            # 必须先 commit 再 close：clear_tenant 的效果在事务里，
            # 若直接 close（等价于 rollback），RESET 会被一并撤销，
            # 连接就会带着上一个请求的租户回到池里。
            try:
                await clear_tenant(session)
                await session.commit()
            except Exception:
                with suppress(Exception):
                    await session.rollback()
            await session.close()


async def get_system_db_session(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """依赖注入：跨租户数据库会话（``app.tenant_id = '*'``）。

    只给确实需要看全部租户的端点用：登录前按邮箱找用户、网关注入校验、
    平台级配额统计、运维巡检。

    与 ``get_db_session`` 的差别不是"能不能查"，而是**代码里看得见**：
    用这个依赖的端点会被 ``tests/test_rls_fail_closed.py`` 的清单门禁逐一登记，
    新增一处跨租户必须同步改清单，评审时不会被悄悄夹带。
    """
    async with async_session_factory() as session:
        await apply_cross_tenant(session)
        log.warning(
            "tenant.cross_tenant_request",
            path=request.url.path,
            method=request.method,
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            # 必须先 commit 再 close：clear_tenant 的效果在事务里，
            # 若直接 close（等价于 rollback），RESET 会被一并撤销，
            # 连接就会带着上一个请求的租户回到池里。
            try:
                await clear_tenant(session)
                await session.commit()
            except Exception:
                with suppress(Exception):
                    await session.rollback()
            await session.close()
