"""
知识推荐 API — 单一职责：提供知识推荐的 HTTP 端点。

端点：
    GET /recommendations/user?top_k=10   — 个性化推荐（猜你想看）
    GET /recommendations/document/{doc_id}?top_k=5 — 相关阅读
    POST /recommendations/feedback        — 行为上报（浏览/收藏/点赞/搜索点击）
    POST /recommendations/rebuild         — 触发离线索引重建（管理员，Celery 异步）

权限：推荐模块经 require_module("knowledge_recommendation") 门控；
    结果统一经 PermissionService.filter_documents 过滤（密级 + 知识库归属）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user, require_module
from app.models.user import User
from app.schemas.common import ApiResponse, BehaviorReport, RecommendationItem
from app.services.permission_service import PermissionService
from app.services.recommendation_service import RecommendationService
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


def _build_service(
    db: AsyncSession,
    request: Request,
) -> RecommendationService:
    """构造推荐服务 — 从 request.state 获取租户 ID。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    return RecommendationService(db, tenant_id=tenant_id)


@router.get("/user", response_model=ApiResponse[list[RecommendationItem]])
async def get_user_recommendations(
    request: Request,
    top_k: int = Query(10, ge=1, le=20, description="返回数量"),
    user: User = Depends(require_module("knowledge_recommendation")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[RecommendationItem]]:
    """个性化推荐（猜你想看）— 结果经权限过滤。"""
    service = _build_service(db, request)
    permission = PermissionService(db, user, tenant_id=getattr(request.state, "tenant_id", None))
    items = await service.recommend_for_user(
        user.id,
        top_k=top_k,
        permission_filter=permission.filter_documents,
    )
    return ApiResponse(code=0, data=items, message="success")


@router.get("/document/{doc_id}", response_model=ApiResponse[list[RecommendationItem]])
async def get_related_documents(
    doc_id: str,
    request: Request,
    top_k: int = Query(5, ge=1, le=20, description="返回数量"),
    current_user: User = Depends(require_module("knowledge_recommendation")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[list[RecommendationItem]]:
    """相关阅读 — 基于当前文档的图谱关联推荐。"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        return ApiResponse(code=400, data=None, message="无效的文档 ID")

    service = _build_service(db, request)
    permission = PermissionService(db, current_user, tenant_id=getattr(request.state, "tenant_id", None))
    items = await service.get_related_documents(
        doc_uuid,
        current_user.id,
        top_k=top_k,
        permission_filter=permission.filter_documents,
    )
    return ApiResponse(code=0, data=items, message="success")


@router.post("/feedback", response_model=ApiResponse[dict])
async def report_behavior(
    request: Request,
    payload: BehaviorReport = Body(...),
    user: User = Depends(require_module("knowledge_recommendation")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict]:
    """行为上报 — 记录浏览/收藏/点赞/搜索点击，作为推荐信号。"""
    try:
        doc_uuid = uuid.UUID(payload.doc_id)
    except ValueError:
        return ApiResponse(code=400, data=None, message="无效的文档 ID")

    service = _build_service(db, request)
    await service.record_behavior(user.id, doc_uuid, payload.action_type)
    await db.commit()
    return ApiResponse(code=0, data={"status": "ok"}, message="success")


@router.post("/rebuild", response_model=ApiResponse[dict])
async def trigger_rebuild(
    request: Request,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict]:
    """触发离线索引重建 — 提交 Celery 任务，需管理员权限。

    返回真实 ``task_id``（Celery AsyncResult.id）。项目暂无 Celery 任务
    状态查询端点（``/mcp/tasks/{task_id}`` 仅服务于 MCP 长耗时工具，
    与 Celery 任务无关），调用方可凭 task_id 通过 Celery 结果后端
    （Redis，result_expires=3600s）查询任务状态。
    """
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    try:
        from tasks.recommendation_tasks import rebuild_recommendation_model

        tenant_id = getattr(request.state, "tenant_id", None)
        async_result = rebuild_recommendation_model.delay(
            tenant_id=str(tenant_id) if tenant_id else None
        )
    except Exception as exc:
        # Celery broker 不可用时不应 500，返回明确错误供管理员排查
        logger.error("recommend.rebuild.submit_failed", error=str(exc))
        return ApiResponse(code=500, data=None, message=f"重建任务提交失败: {exc}")

    return ApiResponse(
        code=0,
        data={"status": "queued", "task_id": async_result.id},
        message="推荐模型重建任务已提交",
    )