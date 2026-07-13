"""
用户反馈路由 — 单一职责：处理用户反馈的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（创建、查询、回复、状态流转）委托给 FeedbackService。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.feedback import (
    FeedbackCreate,
    FeedbackResponse,
    FeedbackStatus,
)
from app.services.feedback_service import FeedbackService

router = APIRouter(prefix="/feedback", tags=["用户反馈"])


class FeedbackRespondBody(BaseModel):
    """回复反馈的请求体。"""

    response: str = Field(..., min_length=1, description="处理回复内容")


class FeedbackStatusBody(BaseModel):
    """更新反馈状态的请求体。"""

    status: FeedbackStatus = Field(..., description="新状态")


@router.post("", response_model=ApiResponse[FeedbackResponse], status_code=201)
async def create_feedback(
    body: FeedbackCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[FeedbackResponse]:
    """创建用户反馈。"""
    service = FeedbackService(db, user)
    feedback = await service.create_feedback(
        type=body.type.value,
        content=body.content,
        related_message_id=body.related_message_id,
    )
    return ApiResponse(
        code=0,
        data=FeedbackResponse.model_validate(feedback),
        message="success",
    )


@router.get("", response_model=ApiResponse[PageResponse[FeedbackResponse]])
async def list_feedback(
    status: FeedbackStatus | None = Query(default=None, description="状态过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[FeedbackResponse]]:
    """分页查询反馈列表，可按状态过滤。"""
    service = FeedbackService(db, user)
    result = await service.list_feedback(
        status=status.value if status else None,
        page=page,
        size=size,
    )
    return ApiResponse(
        code=0,
        data=PageResponse[FeedbackResponse](
            items=[FeedbackResponse.model_validate(f) for f in result.items],
            total=result.total,
            page=result.page,
            size=result.size,
            pages=result.pages,
        ),
        message="success",
    )


@router.put(
    "/{feedback_id}/respond",
    response_model=ApiResponse[FeedbackResponse],
)
async def respond_to_feedback(
    feedback_id: UUID,
    body: FeedbackRespondBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[FeedbackResponse]:
    """回复用户反馈，同时将状态置为 processing。"""
    service = FeedbackService(db, user)
    feedback = await service.respond_to_feedback(feedback_id, body.response)
    return ApiResponse(
        code=0,
        data=FeedbackResponse.model_validate(feedback),
        message="success",
    )


@router.put(
    "/{feedback_id}/status",
    response_model=ApiResponse[FeedbackResponse],
)
async def update_feedback_status(
    feedback_id: UUID,
    body: FeedbackStatusBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[FeedbackResponse]:
    """更新反馈状态。"""
    service = FeedbackService(db, user)
    feedback = await service.update_feedback_status(
        feedback_id, body.status.value
    )
    return ApiResponse(
        code=0,
        data=FeedbackResponse.model_validate(feedback),
        message="success",
    )
