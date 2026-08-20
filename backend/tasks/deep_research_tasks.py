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
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """异步执行课题调研（带里程碑 checkpoint）。

    tenant_id 透传给公网提供商，用于按租户隔离 Tavily 搜索配额；
    progress 回调将进度事件发布到 Redis，供 /research/{task_id}/stream 的 SSE 消费。
    """
    from app.config import get_settings
    from app.llm.factory import get_llm_provider
    from app.rag.retriever import HybridRetriever
    from app.rag.web_search import build_provider
    from app.services.deep_research_service import DeepResearchService
    from app.services.research_progress import EVENT_DONE, publish_progress
    from tasks.milestone_runner import milestone_checkpoint_manager

    llm = get_llm_provider()
    retriever = HybridRetriever()
    s = get_settings()
    web_provider = None
    if s.WEB_SEARCH_ENABLED:
        # 缺 Key / provider 未知时 build_provider 自动回落 MockProvider（不阻塞）
        web_provider = build_provider(s.WEB_SEARCH_PROVIDER, s.WEB_SEARCH_API_KEY)

    async def _progress(event: dict) -> None:
        await publish_progress(task_id, event)

    service = DeepResearchService(llm, retriever, web_provider=web_provider)

    async with milestone_checkpoint_manager() as mgr:
        report = await service.research(
            goal,
            kb_ids=kb_ids,
            checkpoint_manager=mgr,
            task_id=task_id,
            tenant_id=tenant_id,
            progress=_progress,
        )
    await publish_progress(task_id, {"type": EVENT_DONE, "task_id": task_id})
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
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """课题调研长任务 — 里程碑断点恢复 + Celery 重试。"""
        try:
            return _run_async(
                _deep_research_async(self.request.id, goal, kb_ids, tenant_id)
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
