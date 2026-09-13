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
    from app.services.research_progress import (
        EVENT_DONE,
        publish_progress,
        save_failure,
        save_result,
    )
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

    # Celery 结果后端为 rpc://（task_ignore_result=True），最终报告不落 Celery；
    # 成功/失败均持久化到 Redis，供 /research/{task_id}/result 查询。
    try:
        async with milestone_checkpoint_manager() as mgr:
            report = await service.research(
                goal,
                kb_ids=kb_ids,
                checkpoint_manager=mgr,
                task_id=task_id,
                tenant_id=tenant_id,
                progress=_progress,
            )
    except Exception as exc:
        await save_failure(task_id, str(exc))
        # 幂等收尾：标记 research_jobs 为 failed，释放该幂等键供原键重试
        from app.services.research_job_service import mark_research_job_status

        await mark_research_job_status(task_id, "failed", str(exc)[:200])
        raise
    await publish_progress(task_id, {"type": EVENT_DONE, "task_id": task_id})
    data = report.to_dict()
    await save_result(task_id, data)
    # 幂等收尾：标记成功并把报告同条 UPDATE 落库（DB 为权威存储，P0-1；
    # Redis 结果降级为查询缓存与自愈来源）。成功任务继续持有键，重试返回原任务。
    from app.services.research_job_service import mark_research_job_status

    await mark_research_job_status(task_id, "success", output=data)
    return data


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
            # 重试额度耗尽才标记 failed 释放幂等键；仍有重试额度时
            # 任务保持 queued（checkpoint 恢复后重跑，键继续占位）
            if self.request.retries >= self.max_retries:
                try:
                    from app.services.research_job_service import (
                        mark_research_job_status,
                    )

                    _run_async(
                        mark_research_job_status(
                            self.request.id, "failed", str(exc)[:200]
                        )
                    )
                except Exception as mark_exc:
                    logger.warning(
                        "research_job.mark_failed_error",
                        task_id=self.request.id,
                        error=str(mark_exc)[:200],
                    )
            raise self.retry(exc=exc)

except ImportError:
    logger.warning("deep_research.celery_unavailable")
