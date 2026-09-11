"""任务派发助手（轻量 Outbox 侧）— 派发失败持久化"欠投递"记录。

解决的问题：一次性触发链路（好评→FAQ 回流、采纳→FAQ 回流、
解析→智能处理链、FAQ 文档索引、视频→智能处理链）原先
"派发失败仅日志、不影响主流程"，恢复只能靠再次触发 ——
即"把恢复的开关留在下一次点击上"。

本模块提供 dispatch_with_outbox：
    1. 正常路径：直接 .delay()，零额外开销（不写 outbox）；
    2. 派发失败：以独立短事务写入 task_outbox 表（欠投递记录），
       由 tasks.scheduled_tasks.flush_task_outbox 定时补投。

遵循单一职责：仅做"派发 + 失败落箱"，不涉及任务执行语义。
遵循优雅降级：写 outbox 自身失败仅日志，不阻断主流程。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.utils.logger import get_logger

log = get_logger(__name__)

# task_outbox 单行 kwargs 序列化上限保护：超限截断记录（防毒丸），
# 正常触发链路参数均为 ID 字符串，远低于该上限
_MAX_KWARGS_JSON_CHARS: int = 64 * 1024


async def dispatch_with_outbox(task: Any, **kwargs: Any) -> bool:
    """尝试 Celery 派发；失败写 task_outbox 欠投递记录（轻量 Outbox）。

    Args:
        task: Celery 任务对象（需具备 .name 与 .delay）。
        **kwargs: 派发参数（JSONB 可序列化）。

    Returns:
        True = 派发成功；False = 派发失败（已落箱待补投，或落箱也失败）。
    """
    task_name = getattr(task, "name", "") or str(task)
    try:
        task.delay(**kwargs)
        return True
    except Exception as exc:
        log.warning(
            "outbox.dispatch_failed_recorded",
            task_name=task_name,
            error=str(exc)[:200],
        )
        await record_failed_dispatch(task_name, kwargs, exc)
        return False


async def record_failed_dispatch(
    task_name: str, task_kwargs: dict[str, Any], exc: Exception
) -> bool:
    """以独立短事务写入欠投递记录 — 主流程之外的兜底路径，失败仅日志。

    Args:
        task_name: Celery 任务全名。
        task_kwargs: 派发参数。
        exc: 派发异常。

    Returns:
        True = 落箱成功；False = 落箱失败（broker 与 DB 同时不可用的极端场景，
        此时该次触发彻底丢失，仅能靠业务侧再次触发）。
    """
    try:
        import json

        kwargs_json = json.dumps(task_kwargs, ensure_ascii=False, default=str)
        if len(kwargs_json) > _MAX_KWARGS_JSON_CHARS:
            log.error(
                "outbox.kwargs_oversize_truncated",
                task_name=task_name,
                size=len(kwargs_json),
            )
            kwargs_json = kwargs_json[:_MAX_KWARGS_JSON_CHARS]

        from app.database import async_session_factory

        async with async_session_factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO task_outbox (task_name, task_kwargs, status, attempts, last_error)
                    VALUES (:task_name, CAST(:task_kwargs AS JSONB), 'pending', 0, :last_error)
                    """
                ),
                {
                    "task_name": task_name[:200],
                    "task_kwargs": kwargs_json,
                    "last_error": str(exc)[:2000],
                },
            )
            await session.commit()
        log.info(
            "outbox.recorded",
            task_name=task_name,
        )
        return True
    except Exception as box_exc:
        log.error(
            "outbox.record_failed",
            task_name=task_name,
            error=str(box_exc)[:200],
        )
        return False


def dispatch_with_outbox_sync(task: Any, **kwargs: Any) -> bool:
    """dispatch_with_outbox 的同步版本 — 供同步 Celery 任务（如视频 finalize）使用。

    语义与异步版本一致：正常路径直接 .delay()；失败以 asyncio.run 复用
    异步落箱实现（Celery 任务内新建事件循环，与项目既有模式一致）。
    """
    task_name = getattr(task, "name", "") or str(task)
    try:
        task.delay(**kwargs)
        return True
    except Exception as exc:
        log.warning(
            "outbox.dispatch_failed_recorded",
            task_name=task_name,
            error=str(exc)[:200],
        )
        import asyncio

        asyncio.run(record_failed_dispatch(task_name, kwargs, exc))
        return False
