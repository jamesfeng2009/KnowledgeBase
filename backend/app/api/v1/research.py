"""
Deep Research API — 触发课题调研长任务（Celery 异步）+ 实时进度 / 结果查询。

端点：
    POST /research                  — 提交调研目标，派发长任务，返回 task_id。
                                      幂等提交：同用户同 idempotency_key 只建
                                      一条任务；超时重试返回原 task_id（200）；
                                      同键不同输入返回 409 IDEMPOTENCY_CONFLICT。
    GET /research/{task_id}/stream  — SSE 实时进度流（事件类型 decomposed /
                                       subtopic / overview / done），断线重连
                                       自动回放快照。
    GET /research/{task_id}/result  — 查询任务最终报告（Redis 持久化结果；
                                       不再依赖 Celery 结果后端）。

`tenant_id` 从中间件写入的 ``request.state.tenant_id`` 取出，作为
``deep_research_task`` 的入参透传，用于公网混合检索（P4）的
按租户搜索配额隔离。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.research_job_service import (
    IdempotencyConflictError,
    compute_input_hash,
    create_research_job,
)
from app.utils.logger import get_logger
from app.utils.sse import sse_response

logger = get_logger(__name__)

router = APIRouter(prefix="/research", tags=["research"])

_IDEM_KEY_MAX_LEN = 128


class ResearchStartRequest(BaseModel):
    """课题调研请求体。"""

    goal: str = Field(..., min_length=2, max_length=500, description="研究目标")
    kb_ids: list[str] | None = Field(
        default=None, description="限定知识库范围（None=全部）"
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=_IDEM_KEY_MAX_LEN,
        description=(
            "幂等键：调用方为本次提交生成，网络超时重试时必须携带原键；"
            "省略则不参与去重。也可通过 Idempotency-Key 请求头传递（优先）。"
        ),
    )


def _extract_idempotency_key(
    request: Request, payload: ResearchStartRequest
) -> str | None:
    """幂等键取值：Idempotency-Key 请求头优先，其次请求体字段。"""
    header_key = (request.headers.get("Idempotency-Key") or "").strip()
    body_key = (payload.idempotency_key or "").strip()
    key = header_key or body_key
    if key and len(key) > _IDEM_KEY_MAX_LEN:
        raise ValueError("idempotency_key 过长（上限 128 字符）")
    return key or None


def _conflict_response() -> ApiResponse[dict]:
    """同键不同输入 — 409 IDEMPOTENCY_CONFLICT。"""
    return ApiResponse(
        code=409,
        data={"error": {"code": "IDEMPOTENCY_CONFLICT"}},
        message="该幂等键已绑定不同的提交内容，重新处理请换新键",
    )


@router.post("", response_model=ApiResponse[dict])
async def start_research(
    request: Request,
    payload: ResearchStartRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict]:
    """提交课题调研 — 派发 Celery 长任务，返回真实 task_id。

    幂等语义：
        - 首次提交：落 research_jobs 记录并以 job.id 为 Celery task_id 派发，
          返回 ``reused=False``；
        - 同键同输入重试（前置查询命中或唯一约束冲突回读）：返回原 task_id，
          ``reused=True``，不重复派发；
        - 同键不同输入：409 IDEMPOTENCY_CONFLICT。

    传参：只做校验与派发，不执行耗时逻辑。任务在 workers 异步运行，
    断点恢复由 mapping checkpoint 承担。
    """
    try:
        idempotency_key = _extract_idempotency_key(request, payload)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))

    # 指纹取请求输入（goal + 客户端 kb_ids），与权限收敛结果解耦
    input_hash = compute_input_hash(payload.goal, payload.kb_ids)

    try:
        from app.services.permission_service import PermissionService

        # P1 权限收敛：worker 无用户上下文，kb_ids 必须在 API 层与
        # 当前用户可访问集合取交集（单一事实来源 get_accessible_kb_ids），
        # 防止客户端传入任意 kb_ids 越权检索私有知识库。
        kb_ids = payload.kb_ids
        try:
            perm_svc = PermissionService(
                db, user, getattr(request.state, "tenant_id", None)
            )
            accessible = await perm_svc.get_accessible_kb_ids()
            if accessible is not None:  # admin 不限制（None）
                accessible_strs = {str(k) for k in accessible}
                if kb_ids:
                    kb_ids = [k for k in kb_ids if k in accessible_strs]
                else:
                    kb_ids = sorted(accessible_strs)
                if not kb_ids:
                    logger.info(
                        "research.no_accessible_kb", user_id=str(user.id)
                    )
        except Exception as perm_exc:
            # 权限收敛失败 — fail-closed：显式置空（无可检索知识库），
            # 不允许回落为客户端原值造成越权。
            logger.error("research.perm_scope_failed", error=str(perm_exc))
            kb_ids = []

        tenant_id = getattr(request.state, "tenant_id", None)

        # 幂等创建：重复提交（查询命中/唯一约束冲突回读）返回原任务
        job, created = await create_research_job(
            db,
            user_id=user.id,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            goal=payload.goal,
            kb_ids=kb_ids,
        )
    except IdempotencyConflictError:
        return _conflict_response()
    except Exception as exc:
        logger.error(
            "research.submit_failed", error=str(exc), goal=payload.goal[:80]
        )
        return ApiResponse(code=500, data=None, message=f"调研任务提交失败: {exc}")

    if not created:
        logger.info(
            "research.reused_existing",
            job_id=str(job.id),
            user_id=str(user.id),
            status=job.status,
        )
        return ApiResponse(
            code=0,
            data={
                "status": job.status,
                "task_id": str(job.id),
                "reused": True,
            },
            message="重复提交：返回已创建的调研任务",
        )

    # 以 job.id 作为 Celery task_id 派发 — 派发成功后事务随依赖收尾提交；
    # broker 不可用则回滚，记录不入库、键不被占用，可原键重试。
    try:
        from tasks.deep_research_tasks import deep_research_task

        deep_research_task.apply_async(
            args=[payload.goal, kb_ids],
            kwargs={
                "tenant_id": str(tenant_id) if tenant_id else None,
            },
            task_id=str(job.id),
        )
    except Exception as exc:
        await db.rollback()
        logger.error(
            "research.submit_failed", error=str(exc), goal=payload.goal[:80]
        )
        return ApiResponse(code=500, data=None, message=f"调研任务提交失败: {exc}")

    logger.info("research.submitted", task_id=str(job.id), user_id=str(user.id))
    return ApiResponse(
        code=0,
        data={"status": "queued", "task_id": str(job.id), "reused": False},
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
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict]:
    """查询调研任务最终结果 — DB 为权威存储，Redis 降级为缓存与自愈来源（P0-1）。

    读取顺序：
        DB status=success → 返回 output_json（Redis 过期/驱逐不影响）；
        DB status=failed  → 返回 last_error；
        DB queued（含执行中）→ 查 Redis 终态结果键自愈回填 DB 后按其返回
            （救 mark 静默失败 / output_json 上线前存量行）；无则 running。

    归属校验：仅任务归属者（或 admin）可查 — 防止枚举 task_id 越权读报告。
    """
    from sqlalchemy import select

    from app.models.research import ResearchJob
    from app.services.research_job_service import backfill_result_from_redis
    from app.services.research_progress import load_result

    try:
        jid = uuid.UUID(task_id)
    except (ValueError, AttributeError):
        return ApiResponse(code=404, data=None, message="调研任务不存在")

    try:
        job = (
            (await db.execute(select(ResearchJob).where(ResearchJob.id == jid)))
            .scalar_one_or_none()
        )
    except Exception as exc:
        logger.error("research.result_query_unavailable", error=str(exc)[:200])
        return ApiResponse(code=503, data=None, message=f"任务结果查询不可用: {exc}")

    if job is None:
        return ApiResponse(code=404, data=None, message="调研任务不存在")
    if job.user_id != user.id and getattr(user, "role", "") != "admin":
        logger.info(
            "research.result_denied", task_id=task_id, user_id=str(user.id)
        )
        return ApiResponse(code=403, data=None, message="无权查看该调研任务")

    if job.status == "success":
        return ApiResponse(
            code=0,
            data={"status": "success", "report": job.output_json or {}},
            message="调研完成",
        )
    if job.status == "failed":
        return ApiResponse(
            code=0,
            data={
                "status": "failed",
                "error": job.last_error or "unknown error",
            },
            message="调研任务失败",
        )

    # queued（含执行中）— Redis 终态自愈回填（仅回填，不改变进行中语义）
    try:
        data = await load_result(task_id)
    except Exception as exc:
        logger.warning("research.result_backfill_read_failed", error=str(exc)[:200])
        data = None
    if data and data.get("status") == "success":
        await backfill_result_from_redis(task_id, data)
        return ApiResponse(
            code=0,
            data={"status": "success", "report": data.get("report", {})},
            message="调研完成",
        )
    if data and data.get("status") == "failed":
        await backfill_result_from_redis(task_id, data)
        return ApiResponse(
            code=0,
            data={"status": "failed", "error": data.get("error", "unknown error")},
            message="调研任务失败",
        )
    return ApiResponse(
        code=0, data={"status": "running"}, message="调研任务进行中"
    )