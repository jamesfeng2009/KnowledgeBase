"""
知识推荐 API — 单一职责：提供知识推荐的 HTTP 端点。

端点：
    GET /recommendations/user?top_k=10   — 个性化推荐（猜你想看）
    GET /recommendations/document/{doc_id}?top_k=5 — 相关阅读
    POST /recommendations/feedback        — 行为上报（浏览/收藏/点赞/搜索点击）
    POST /recommendations/rebuild         — 触发离线索引重建（管理员，预留）

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
    """触发离线索引重建 — 预留端点，需管理员权限。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")
    # TODO(Phase 2): 提交 Celery 任务重建协同过滤相似度矩阵 / 用户偏好向量
    return ApiResponse(code=0, data={"status": "queued"}, message="已提交重建任务（Phase 2 实现）")