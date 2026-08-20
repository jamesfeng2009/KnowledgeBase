"""
长任务里程碑执行器 — P2-13：Celery 长任务按阶段存检查点 + 单步超时分级。

定位：
    多阶段长任务（解析→索引→图谱→终态化、课题调研各子课题等）执行期间，
    每完成一个阶段写入里程碑 checkpoint；失败重试（Celery retry）时
    自动跳过已完成阶段，从最近里程碑恢复，避免重复执行昂贵阶段（LLM/解析）。

超时分级：
    - 单步骤超时：由本执行器用 ``asyncio.wait_for`` 控制
      （默认读 ``settings.TASK_STEP_TIMEOUT_SECONDS``）；
    - 总任务超时：由 Celery 全局 ``task_soft_time_limit`` / ``task_time_limit``
      控制（celery_app.py，1500s/1800s）。
    两级独立管理，单步卡死不必等到总超时才失败。

用法::

    from tasks.milestone_runner import (
        MilestoneStage,
        milestone_checkpoint_manager,
        run_stages_with_milestones,
    )

    async with milestone_checkpoint_manager() as mgr:
        results = await run_stages_with_milestones(
            stages=[
                MilestoneStage("parse", lambda: _parse(doc_id)),
                MilestoneStage("index", lambda: _index(doc_id)),
            ],
            task_id=self.request.id,   # Celery 任务 ID
            checkpoint_manager=mgr,
        )
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.memory.checkpoint import CheckpointManager
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MilestoneStage:
    """里程碑阶段定义。

    Attributes:
        name: 阶段名（里程碑名，断点恢复的去重键）
        run: 无参异步可调用，执行该阶段并返回结果
    """

    name: str
    run: Callable[[], Awaitable[Any]]


def _default_step_timeout_s() -> float:
    """读取单步骤超时配置，异常时兜底 300s。"""
    try:
        from app.config import get_settings

        return float(getattr(get_settings(), "TASK_STEP_TIMEOUT_SECONDS", 300))
    except Exception:
        return 300.0


@asynccontextmanager
async def milestone_checkpoint_manager() -> AsyncIterator[CheckpointManager]:
    """为 Celery 任务创建绑定独立会话的 CheckpointManager。

    复用 ``task_db_session``（每任务独立 session factory，
    避免跨事件循环复用连接）。
    """
    from app.database import task_db_session

    async with task_db_session() as db:
        yield CheckpointManager(db)


async def run_stages_with_milestones(
    stages: list[MilestoneStage],
    *,
    task_id: str,
    checkpoint_manager: CheckpointManager,
    step_timeout_s: float | None = None,
    resume: bool = True,
    on_stage: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """按序执行阶段并逐阶段写里程碑，支持断点恢复与单步超时。

    Args:
        stages: 有序阶段列表
        task_id: Celery 任务 ID（内部加 "task:" 前缀作为 checkpoint key）
        checkpoint_manager: 注入的 CheckpointManager（测试可传 Fake）
        step_timeout_s: 单步骤超时秒数；None 时读配置 TASK_STEP_TIMEOUT_SECONDS
        resume: True 时跳过已有 done 里程碑的阶段（失败重试场景）
        on_stage: 可选进度回调，每阶段收尾时调用
            ``await on_stage(stage_name, {"status": ..., ...})``；
            status ∈ "skip_done" | "done" | "timeout"。None 时不触发（默认）。

    Returns:
        {阶段名: 阶段返回值} — 含断点恢复时从里程碑还原的已跳过阶段结果

    Raises:
        TimeoutError: 某阶段超过单步超时（记录 timeout 里程碑后抛出，
            交由 Celery retry；重试时该阶段因无 done 里程碑会重跑）
    """
    if step_timeout_s is None:
        step_timeout_s = _default_step_timeout_s()

    checkpoint_key = f"task:{task_id}"
    completed: set[str] = set()
    persisted: dict[str, Any] = {}
    if resume:
        milestones = await checkpoint_manager.get_milestones(checkpoint_key)
        for m in milestones:
            detail = m.get("detail", {})
            if detail.get("status") != "done":
                continue
            name = m.get("name", "")
            completed.add(name)
            # 还原持久化的阶段结果 — 被跳过阶段无需重跑即可取回产出
            if "result" in detail:
                persisted[name] = detail["result"]
        if completed:
            logger.info(
                "milestone_runner.resume",
                task_id=task_id,
                completed=sorted(completed),
            )

    results: dict[str, Any] = {}
    for stage in stages:
        if stage.name in completed:
            logger.info(
                "milestone_runner.skip_done",
                task_id=task_id,
                stage=stage.name,
            )
            if stage.name in persisted:
                results[stage.name] = persisted[stage.name]
            if on_stage is not None:
                await on_stage(stage.name, {"status": "skip_done"})
            continue

        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(stage.run(), timeout=step_timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            # 单步超时 — 记录 timeout 里程碑（非 done，重试时会重跑）后抛出
            await checkpoint_manager.save_milestone(
                checkpoint_key,
                stage.name,
                detail={
                    "status": "timeout",
                    "step_timeout_s": step_timeout_s,
                },
            )
            logger.warning(
                "milestone_runner.step_timeout",
                task_id=task_id,
                stage=stage.name,
                step_timeout_s=step_timeout_s,
            )
            if on_stage is not None:
                await on_stage(
                    stage.name,
                    {"status": "timeout", "step_timeout_s": step_timeout_s},
                )
            raise

        duration_ms = round((time.monotonic() - t0) * 1000, 2)
        detail: dict[str, Any] = {"status": "done", "duration_ms": duration_ms}
        # 结果可 JSON 序列化时随里程碑持久化，断点恢复后可直接还原
        try:
            json.dumps(result, ensure_ascii=False, default=str)
            detail["result"] = result
        except (TypeError, ValueError):
            pass
        await checkpoint_manager.save_milestone(
            checkpoint_key,
            stage.name,
            detail=detail,
        )
        results[stage.name] = result
        logger.info(
            "milestone_runner.stage_done",
            task_id=task_id,
            stage=stage.name,
            duration_ms=duration_ms,
        )
        if on_stage is not None:
            await on_stage(
                stage.name,
                {"status": "done", "duration_ms": duration_ms, "result": result},
            )

    return results
