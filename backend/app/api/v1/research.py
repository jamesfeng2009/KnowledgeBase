"""
Deep Research API — 触发课题调研长任务（Celery 异步）+ 实时进度 / 结果查询。

端点：
    POST /research                  — 提交调研目标，派发长任务，返回 task_id。
    GET /research/{task_id}/stream  — SSE 实时进度流（事件类型 decomposed /
                                       subtopic / overview / done），断线重连
                                       自动回放快照。
    GET /research/{task_id}/result  — 查询任务最终报告（Celery 结果后端）。

`tenant_id` 从中间件写入的 ``request.state.tenant_id`` 取出，作为
``deep_research_task.delay`` 的入参透传，用于公网混合检索（P4）的
按租户搜索配额隔离。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger
from app.utils.sse import sse_response

logger = get_logger(__name__)

router = APIRouter(prefix="/research", tags=["research"])


class ResearchStartRequest(BaseModel):
    """课题调研请求体。"""

    goal: str = Field(..., min_length=2, max_length=500, description="研究目标")
    kb_ids: list[str] | None = Field(
        default=None, description="限定知识库范围（None=全部）"
    )


@router.post("", response_model=ApiResponse[dict])
async def start_research(
    request: Request,
    payload: ResearchStartRequest,
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """提交课题调研 — 派发 Celery 长任务，返回真实 task_id。

    传参：只做校验与派发，不执行耗时逻辑。任务在 workers 异步运行，
    断点恢复由 mapping checkpoint 承担，结果经 Celery 结果后端查询。
    """
    try:
        from tasks.deep_research_tasks import deep_research_task

        tenant_id = getattr(request.state, "tenant_id", None)
        async_result = deep_research_task.delay(
            payload.goal,
            payload.kb_ids,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
    except Exception as exc:
        # Celery broker 不可用时不应 500，返回明确错误供排查
        logger.error("research.submit_failed", error=str(exc), goal=payload.goal[:80])
        return ApiResponse(code=500, data=None, message=f"调研任务提交失败: {exc}")

    logger.info("research.submitted", task_id=async_result.id, user_id=str(user.id))
    return ApiResponse(
        code=0,
        data={"status": "queued", "task_id": async_result.id},
        message="调研任务已提交",
    )


@router.get("/{task_id}/stream")
async def research_stream(
    task_id: str,
    user: User = Depends(get_current_active_user),
):
    """SSE 实时调研进度流。

    事件（按顺序出现）：decomposed → subtopic（每个子课题一条）→ overview →
    done。断线重连 / 关标签页重开后，服务端先回放该 task 的快照再继续实时事件。
    """
    from app.services.research_progress import subscribe_stream

    logger.info("research.stream_open", task_id=task_id, user_id=str(user.id))
    return sse_response(subscribe_stream(task_id))


@router.get("/{task_id}/result", response_model=ApiResponse[dict])
async def get_research_result(
    task_id: str,
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """查询调研任务最终结果（Celery 结果后端）。

    前端通常在收到 SSE 的 done 事件后调用本端点拉取完整报告。
    """
    try:
        from celery.result import AsyncResult

        from celery_app import celery_app

        ar: AsyncResult = AsyncResult(task_id, app=celery_app)
    except Exception as exc:
        logger.error("research.result_query_unavailable", error=str(exc)[:200])
        return ApiResponse(code=503, data=None, message=f"任务结果查询不可用: {exc}")

    state = ar.state
    if state == "SUCCESS":
        report = ar.result
        if not isinstance(report, dict):
            report = {}
        return ApiResponse(
            code=0,
            data={"status": "success", "report": report},
            message="调研完成",
        )
    if state == "FAILURE":
        try:
            exc_info = str(ar.info).splitlines()[0] if ar.info else "unknown error"
        except Exception:
            exc_info = "unknown error"
        return ApiResponse(
            code=0,
            data={"status": "failed", "error": exc_info},
            message="调研任务失败",
        )
    # PENDING / STARTED / RETRY 等
    return ApiResponse(
        code=0,
        data={"status": state.lower()},
        message="调研任务进行中",
    )