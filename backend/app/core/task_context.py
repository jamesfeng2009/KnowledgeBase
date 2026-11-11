"""
Celery 任务租户上下文 — 把入队时带上的 ``tenant_id`` 自动带到任务内的会话。

## 解决什么

RLS fail-closed 之后，任务里的 ``task_db_session()`` 必须知道"我是谁"。
但任务的租户是**入队时**才确定的，而 ``task_db_session()`` 在任务体深处被调用，
一路传参要改几十个函数签名。

这里用 ``ContextVar`` 做一次传递：``task_prerun`` 信号从任务参数里取出
``tenant_id`` 存进去，``task_db_session()`` 无参调用时读出来。既不侵入签名，
也不依赖全局状态（ContextVar 是每个执行上下文独立的，不会跨任务串）。

## 边界

- 只在**同一个 worker 进程的同一执行上下文**内有效；Celery 每个任务独立执行，
  不会互相污染。
- 拿不到 ``tenant_id`` 时保持为空 —— ``task_db_session()`` 于是不设 GUC，
  RLS fail-closed，任务查不到任何数据。**不会**退回全可见。
- 系统级任务（跨租户巡检、配额统计）不该依赖这里，应显式传
  ``cross_tenant_reason``，让越权在代码里可见。
"""

from __future__ import annotations

import inspect
from contextvars import ContextVar
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

_current_tenant: ContextVar[str | None] = ContextVar(
    "ekb_task_tenant_id", default=None
)
_current_task_name: ContextVar[str | None] = ContextVar(
    "ekb_task_name", default=None
)


def bind_task_tenant(tenant_id: Any) -> None:
    """把租户写入当前执行上下文。``None``/空串表示"没有租户身份"。"""
    if tenant_id is None or (isinstance(tenant_id, str) and not tenant_id.strip()):
        _current_tenant.set(None)
    else:
        _current_tenant.set(str(tenant_id).strip())


def current_task_tenant() -> str | None:
    """读取当前任务的租户 ID，无则 None。"""
    return _current_tenant.get()


def current_task_name() -> str | None:
    return _current_task_name.get()


def reset_task_tenant() -> None:
    _current_tenant.set(None)
    _current_task_name.set(None)


def extract_tenant_from_task(
    task: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str | None:
    """从任务调用参数里取 ``tenant_id``。

    任务的 ``tenant_id`` 位置不固定（有的在第二位，有的在最后，有的只有
    ``**kwargs``），所以按函数签名绑定而不是按位置猜。绑定失败就算了 ——
    这个函数的职责是"尽量拿到"，拿不到由下游显式处理，不该因为猜错而抛异常
    把任务搞挂。
    """
    if kwargs and kwargs.get("tenant_id"):
        return str(kwargs["tenant_id"]).strip()

    run = getattr(task, "run", None) or task
    try:
        sig = inspect.signature(run)
        # bind=True 的任务，run 的第一个参数是 self，而 Celery 传给 signal 的
        # args 不含它；用 bind_partial 容忍数量不匹配
        bound = sig.bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        return None

    value = bound.arguments.get("tenant_id")
    if value is None:
        return None
    return str(value).strip() or None
