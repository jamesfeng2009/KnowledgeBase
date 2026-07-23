"""HTTP 请求上下文 — 基于 contextvars 传递 request_id，供日志、追踪和用量记录关联。

设计要点：
    - 中间件在每个请求开始时生成 request_id 并写入 contextvars
    - 引擎、服务层通过 ``get_request_id()`` 读取，无需修改函数签名层层透传
    - structlog 也绑定同一 request_id，使日志条目自动携带
    - LangFuse Trace 的 metadata 中包含 request_id，实现链路关联

使用示例::

    from app.utils.request_context import get_request_id

    rid = get_request_id()  # 当前请求的 request_id，或 None
"""

from __future__ import annotations

import contextvars

# 当前请求的 request_id（由中间件设置，供引擎/服务层读取）
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def get_request_id() -> str | None:
    """获取当前请求的 request_id。

    Returns:
        request_id 字符串，或 None（非 HTTP 请求上下文，如 Celery 任务）。
    """
    return request_id_var.get()


def set_request_id(rid: str | None) -> contextvars.Token[str | None]:
    """设置当前请求的 request_id。

    Args:
        rid: request_id 字符串。

    Returns:
        contextvars Token，供 ``reset_request_id()`` 恢复。
    """
    return request_id_var.set(rid)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    """恢复 request_id 到之前的值（中间件 finally 中调用）。"""
    request_id_var.reset(token)
