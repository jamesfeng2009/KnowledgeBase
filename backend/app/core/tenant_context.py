"""
租户上下文注入 — RLS fail-closed 之后**唯一**允许写 GUC 的地方。

## 为什么需要这个模块

RLS 策略此前是 fail-open 的：

    current_setting('app.tenant_id', true) IS NULL
    OR tenant_id::text = current_setting('app.tenant_id', true)

GUC 没设置时第一个分支恒真 → 全表可见。任何绕过 TenantContextMiddleware
的入口（Celery 任务、scripts、单租户兜底）都能读全租户数据，而且**不报错**，
所以这条洞能长期潜伏。

迁移 `e8f9a0b1c2d3` 把它改成 fail-closed：

    current_setting('app.tenant_id', true) = '*'                    -- 显式跨租户
    OR tenant_id::text = current_setting('app.tenant_id', true)     -- 严格租户

GUC 未设置时两边都是 NULL → 一行都看不见。于是**每个**访问数据库的入口都必须
显式声明身份。本模块就是这个声明的唯一入口，避免各处手写 SET 语句（写漏一处
就是一次数据错乱，而且不报错）。

## 三种身份

| 场景 | API | GUC |
|------|-----|-----|
| 请求/任务属于某个租户（绝大多数） | ``tenant_scope(session, tid)`` | `<uuid>` |
| 跨租户系统操作（对账、配额、反查归属） | ``system_scope(session, reason=...)`` | ``*`` |
| 什么都不设 | —— | 空 → **拒绝所有行** |

``system_scope`` 强制要求 ``reason`` 并写 ``tenant.cross_tenant_scope`` 审计日志。
跨租户本身不是错误，但必须是**可数的、有理由的**，否则等于把 fail-open 换个地方放回来。

## 为什么用 set_config 而不是 SET LOCAL

``SET LOCAL`` 的作用域是**当前事务**。同一个 session 只要 commit 过一次，GUC 就失效，
之后那次查询在 fail-closed 下静默返回空集 —— 症状是"偶尔查不到数据"，极难排查。
``set_config(name, value, false)`` 是 session 级、跨事务保持；配合 ``NullPool``
（每会话一条新连接、用完即关）不会泄漏到下一个请求。

即便如此，本模块仍在退出时显式 RESET：将来若把 NullPool 换成连接池，
漏掉这步就是跨请求串租户。
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.logger import get_logger

log = get_logger(__name__)

#: RLS 策略读取的 GUC 名，必须与迁移中的 ``current_setting('app.tenant_id', true)`` 一致
TENANT_GUC = "app.tenant_id"

#: 跨租户哨兵值。只允许 ``system_scope`` 写入，``tenant_scope`` 会拒绝
CROSS_TENANT = "*"

# is_local=false → session 级，跨事务保持（SET LOCAL 会被 commit 清掉，见模块 docstring）
_SET_SQL = text("SELECT set_config(:name, :value, false)")


class TenantContextError(RuntimeError):
    """租户上下文非法：缺租户、格式错、缺 reason。

    刻意不用 ValueError：``app.main`` 把 ValueError 全局映射成 404，
    上下文错误应该是 500 级（服务端漏了注入），不是"资源不存在"。
    """


def normalize_tenant_id(value: Any) -> str:
    """校验并归一化租户 ID，返回小写 UUID 字符串。

    RLS 策略用 ``tenant_id::text = current_setting(...)`` 比较，PG 输出 uuid
    是小写；这里统一小写，避免大小写不一致导致"明明设了却查不到"。

    Raises:
        TenantContextError: 值为空、是跨租户哨兵、或不是合法 UUID。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise TenantContextError(
            "租户 ID 为空。fail-closed 模式下必须显式指定租户；"
            "确需跨租户请用 system_scope(reason=...)"
        )
    raw = str(value).strip()
    if raw == CROSS_TENANT:
        raise TenantContextError(
            "跨租户哨兵 '*' 只能由 system_scope() 使用，tenant_scope() 不接受"
        )
    try:
        return str(uuid.UUID(raw)).lower()
    except (ValueError, AttributeError, TypeError) as exc:
        raise TenantContextError(f"租户 ID 不是合法 UUID: {value!r}") from exc


async def apply_tenant(session: AsyncSession, tenant_id: Any) -> str:
    """把租户 ID 写入会话 GUC，返回归一化后的租户 ID。"""
    tid = normalize_tenant_id(tenant_id)
    await session.execute(_SET_SQL, {"name": TENANT_GUC, "value": tid})
    return tid


async def apply_cross_tenant(session: AsyncSession) -> None:
    """把会话 GUC 设为跨租户哨兵。仅 ``system_scope`` 应调用它。"""
    await session.execute(_SET_SQL, {"name": TENANT_GUC, "value": CROSS_TENANT})


async def clear_tenant(session: AsyncSession) -> None:
    """清空 GUC。

    清空后策略两边都是 NULL → 拒绝所有行。这是**安全的默认状态**，
    不是"回到 fail-open"。
    """
    await session.execute(_SET_SQL, {"name": TENANT_GUC, "value": ""})


@asynccontextmanager
async def tenant_scope(session: AsyncSession, tenant_id: Any) -> AsyncIterator[str]:
    """在严格租户作用域内执行，退出时清空 GUC。

    Example::

        async with tenant_scope(session, tenant_id):
            await repo.list_documents()
    """
    tid = await apply_tenant(session, tenant_id)
    try:
        yield tid
    finally:
        await clear_tenant(session)


@asynccontextmanager
async def system_scope(session: AsyncSession, *, reason: str) -> AsyncIterator[None]:
    """在跨租户作用域内执行。``reason`` 必填并会写入审计日志。

    用于确实需要看到全部租户的系统路径：配额统计、补偿扫描、按主键反查归属。
    业务请求路径**不应**使用它 —— 那等于把 fail-open 换个文件名放回来。

    Raises:
        TenantContextError: ``reason`` 为空。
    """
    cleaned = (reason or "").strip()
    if not cleaned:
        raise TenantContextError(
            "system_scope 必须提供 reason —— 跨租户访问必须可审计"
        )
    await apply_cross_tenant(session)
    log.warning(
        "tenant.cross_tenant_scope",
        reason=cleaned,
        pid=os.getpid(),
    )
    try:
        yield
    finally:
        await clear_tenant(session)


async def resolve_tenant_of_row(
    session: AsyncSession,
    *,
    table: str,
    row_id: Any,
    reason: str,
    required: bool = False,
) -> str | None:
    """按主键反查某行所属租户（``system_scope`` 内，只 SELECT 一列）。

    跨租户只发生在这一条查询上：查完立刻退出跨租户作用域，业务逻辑仍在
    调用方指定的租户下运行。暴露面被压到最小。

    Args:
        table: 表名（**不接受用户输入**，只能是代码里写死的常量）。
        row_id: 主键值（UUID）。
        reason: 审计理由。

    Args:
        required: 行不存在时是否抛异常。默认 False——反查只是"尽力确定身份"，
            查不到时应让调用方按原有逻辑早退（例如"文档不存在 → 返回 done"），
            而不是把一次正常的分支变成 500。调用方拿到 None 就表示"没身份"，
            此时会话保持 fail-closed，后续查询同样返回空，语义自洽。

    Raises:
        TenantContextError: ``required=True`` 且行不存在，或该行 ``tenant_id``
            为 NULL。NULL 的情况在 fail-closed 下无解 —— RLS 策略里
            ``tenant_id::text = guc`` 对 NULL 恒为 NULL，这类行看不见也改不了。
    """
    if not table.replace("_", "").isalnum():
        raise TenantContextError(f"非法表名: {table!r}")

    try:
        rid = str(uuid.UUID(str(row_id).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TenantContextError(f"主键不是合法 UUID: {row_id!r}") from exc

    found: Any = None
    try:
        async with system_scope(session, reason=reason):
            row = await session.execute(
                text(f'SELECT tenant_id FROM public."{table}" WHERE id = :rid'),
                {"rid": rid},
            )
            found = row.scalar_one_or_none()
        # 归一化也放进 try：DDL 上 tenant_id 是 uuid 列，正常不会非法；
        # 真拿到非法值时说明连接/SQL 层已经坏了，按"拿不到身份"处理更安全
        if found is not None:
            return normalize_tenant_id(found)
    except Exception as exc:  # noqa: BLE001
        # 反查失败等价于"拿不到身份"，安全侧仍然是 fail-closed（查不到数据），
        # 不会因为这里降级而泄漏。所以不让它把整个任务打成 500；
        # 但必须留 warning —— 真实 SQL 故障要靠这条日志被发现，
        # 否则表现成"数据不存在"，比报错更难查。
        log.warning(
            "tenant.resolve_owner_failed",
            table=table,
            row_id=str(row_id),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None

    if required:
        raise TenantContextError(
            f"{table} 中按 id={row_id} 找不到行，或该行 tenant_id 为 NULL "
            f"（fail-closed 下这类行不可访问）"
        )
    log.debug("tenant.resolve_owner_missing", table=table, row_id=str(row_id))
    return None


async def resolve_task_tenant(
    session: AsyncSession,
    *,
    tenant_id: Any = None,
    doc_id: Any = None,
    reason: str = "celery.task.resolve_tenant",
    required: bool = False,
) -> str | None:
    """Celery 任务入口用：确定本次任务该以哪个租户身份运行。

    优先级：
    1. 调用方传入的 ``tenant_id``（正解 —— 入队时就该带上）；
    2. 反查 ``documents.tenant_id``（兼容历史队列消息、老调用点）。

    Returns:
        归一化的租户 ID；反查不到且 ``required=False`` 时返回 None
        （调用方应让会话保持 fail-closed 并按原有逻辑处理"不存在"）。

    Raises:
        TenantContextError: 两者都没给出（配置错误，必须炸），
            或 ``required=True`` 且反查不到。
    """
    if tenant_id is not None and str(tenant_id).strip():
        return normalize_tenant_id(tenant_id)

    if doc_id is None or not str(doc_id).strip():
        raise TenantContextError(
            "任务既没有 tenant_id 也没有 doc_id，无法确定租户上下文"
        )
    return await resolve_tenant_of_row(
        session, table="documents", row_id=doc_id, reason=reason, required=required
    )
