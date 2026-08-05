"""
Deep Research Celery 任务 — P2-11：课题调研长任务异步执行。

将耗时的多子课题调研（检索 + LLM 归纳 + 矛盾检测）从 HTTP 请求剥离，
结合 P2-13 里程碑 checkpoint：失败重试时跳过已完成子课题。

在 ``celery_app`` 不可用时（如开发环境）优雅降级，仅输出告警日志。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _run_async(coro):
    """在同步 Celery 任务中执行异步协程。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _deep_research_async(
    task_id: str,
    goal: str,
    kb_ids: list[str] | None,
) -> dict[str, Any]:
    """异步执行课题调研（带里程碑 checkpoint）。"""
    from app.llm.factory import get_llm_provider
    from app.rag.retriever import HybridRetriever
    from app.services.deep_research_service import DeepResearchService
    from tasks.milestone_runner import milestone_checkpoint_manager

    llm = get_llm_provider()
    retriever = HybridRetriever()
    service = DeepResearchService(llm, retriever)

    async with milestone_checkpoint_manager() as mgr:
        report = await service.research(
            goal,
            kb_ids=kb_ids,
            checkpoint_manager=mgr,
            task_id=task_id,
        )
    return report.to_dict()


try:
    from celery_app import celery_app

    @celery_app.task(
        bind=True,
        name="tasks.deep_research_tasks.deep_research_task",
        max_retries=2,
    )
    def deep_research_task(
        self,
        goal: str,
        kb_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """课题调研长任务 — 里程碑断点恢复 + Celery 重试。"""
        try:
            return _run_async(
                _deep_research_async(self.request.id, goal, kb_ids)
            )
        except Exception as exc:
            logger.warning(
                "deep_research.task_failed",
                task_id=self.request.id,
                error=str(exc)[:200],
            )
            raise self.retry(exc=exc)

except ImportError:
    logger.warning("deep_research.celery_unavailable")
