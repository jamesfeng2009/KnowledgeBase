"""
文档评论路由 — 单一职责：处理文档评论的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（创建、查询、解决评论）委托给 CommentService。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.comment import CommentCreate, CommentResponse
from app.schemas.common import ApiResponse
from app.services.comment_service import CommentService

router = APIRouter(tags=["文档评论"])


@router.get(
    "/documents/{doc_id}/comments",
    response_model=ApiResponse[list[CommentResponse]],
)
async def list_comments(
    request: Request,
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[CommentResponse]]:
    """查询指定文档下的顶层评论列表。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = CommentService(db, user, tenant_id=tenant_id)
    comments = await service.list_comments(doc_id)
    return ApiResponse(
        code=0,
        data=[CommentResponse.model_validate(c) for c in comments],
        message="success",
    )


@router.post(
    "/documents/{doc_id}/comments",
    response_model=ApiResponse[CommentResponse],
    status_code=201,
)
async def create_comment(
    request: Request,
    doc_id: UUID,
    body: CommentCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[CommentResponse]:
    """在文档下发表评论或回复。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = CommentService(db, user, tenant_id=tenant_id)
    comment = await service.create_comment(
        doc_id=doc_id,
        content=body.content,
        parent_id=body.parent_id,
    )
    return ApiResponse(
        code=0,
        data=CommentResponse.model_validate(comment),
        message="success",
    )


@router.put(
    "/comments/{comment_id}/resolve",
    response_model=ApiResponse[CommentResponse],
)
async def resolve_comment(
    request: Request,
    comment_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[CommentResponse]:
    """标记评论为已解决（仅评论作者或 admin）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = CommentService(db, user, tenant_id=tenant_id)
    comment = await service.resolve_comment(comment_id)
    return ApiResponse(
        code=0,
        data=CommentResponse.model_validate(comment),
        message="success",
    )
