"""
问答社区路由 — 单一职责：处理问答帖与回答的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（创建、浏览计数、采纳逻辑）委托给 QaService。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.qa import (
    QaAnswerCreate,
    QaAnswerResponse,
    QaQuestionCreate,
    QaQuestionResponse,
    QaQuestionStatus,
)
from app.services.qa_service import QaService

router = APIRouter(prefix="/qa", tags=["问答社区"])


@router.get(
    "/questions",
    response_model=ApiResponse[PageResponse[QaQuestionResponse]],
)
async def list_questions(
    status: QaQuestionStatus | None = Query(default=None, description="状态过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[QaQuestionResponse]]:
    """分页查询问题列表，可按状态过滤。"""
    service = QaService(db, user)
    result = await service.list_questions(
        status=status.value if status else None,
        page=page,
        size=size,
    )
    return ApiResponse(
        code=0,
        data=PageResponse[QaQuestionResponse](
            items=[QaQuestionResponse.model_validate(q) for q in result.items],
            total=result.total,
            page=result.page,
            size=result.size,
            pages=result.pages,
        ),
        message="success",
    )


@router.post(
    "/questions",
    response_model=ApiResponse[QaQuestionResponse],
    status_code=201,
)
async def create_question(
    body: QaQuestionCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[QaQuestionResponse]:
    """创建问答帖。"""
    service = QaService(db, user)
    question = await service.create_question(
        kb_id=body.kb_id,
        title=body.title,
        content=body.content,
        tags=body.tags,
    )
    return ApiResponse(
        code=0,
        data=QaQuestionResponse.model_validate(question),
        message="success",
    )


@router.get(
    "/questions/{question_id}",
    response_model=ApiResponse[QaQuestionResponse],
)
async def get_question(
    question_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[QaQuestionResponse]:
    """获取问题详情（浏览数自动 +1）。"""
    service = QaService(db, user)
    question = await service.get_question(question_id)
    return ApiResponse(
        code=0,
        data=QaQuestionResponse.model_validate(question),
        message="success",
    )


@router.post(
    "/questions/{question_id}/answers",
    response_model=ApiResponse[QaAnswerResponse],
    status_code=201,
)
async def create_answer(
    question_id: UUID,
    body: QaAnswerCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[QaAnswerResponse]:
    """为指定问题创建回答。"""
    service = QaService(db, user)
    answer = await service.create_answer(
        question_id=question_id,
        content=body.content,
        is_ai_generated=body.is_ai_generated,
    )
    return ApiResponse(
        code=0,
        data=QaAnswerResponse.model_validate(answer),
        message="success",
    )


@router.put(
    "/answers/{answer_id}/accept",
    response_model=ApiResponse[QaAnswerResponse],
)
async def accept_answer(
    answer_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[QaAnswerResponse]:
    """采纳回答（仅问题作者或 admin 可操作）。"""
    service = QaService(db, user)
    answer = await service.accept_answer(answer_id)
    return ApiResponse(
        code=0,
        data=QaAnswerResponse.model_validate(answer),
        message="success",
    )
