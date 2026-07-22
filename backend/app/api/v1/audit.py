"""
审核流程路由 — 单一职责：处理审核提交、查询与审批的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（提交审核、待审核列表、通过/驳回流转）委托给 AuditService。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.audit import (
    AuditFlowResponse,
    AuditPriority,
    ResourceType,
)
from app.schemas.common import ApiResponse, PageResponse
from app.services.audit_service import AuditService

router = APIRouter(prefix="/audit", tags=["审核流程"])


class AuditSubmitBody(BaseModel):
    """提交审核的请求体。"""

    resource_type: ResourceType = Field(..., description="资源类型")
    resource_id: UUID = Field(..., description="资源 ID")
    priority: AuditPriority = Field(
        default=AuditPriority.normal, description="优先级"
    )


class AuditCommentBody(BaseModel):
    """审核意见请求体（通过/驳回通用）。"""

    comment: str | None = Field(default=None, description="审核意见")


@router.post("", response_model=ApiResponse[AuditFlowResponse], status_code=201)
async def submit_for_review(
    request: Request,
    body: AuditSubmitBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[AuditFlowResponse]:
    """提交资源审核。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = AuditService(db, user, tenant_id=tenant_id)
    audit = await service.submit_for_review(
        resource_type=body.resource_type.value,
        resource_id=body.resource_id,
        priority=body.priority.value,
    )
    return ApiResponse(
        code=0,
        data=AuditFlowResponse.model_validate(audit),
        message="success",
    )


@router.get(
    "/pending",
    response_model=ApiResponse[PageResponse[AuditFlowResponse]],
)
async def list_pending(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[AuditFlowResponse]]:
    """分页查询待审核列表（按优先级降序 + 创建时间升序）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = AuditService(db, user, tenant_id=tenant_id)
    result = await service.list_pending(page=page, size=size)
    return ApiResponse(
        code=0,
        data=PageResponse[AuditFlowResponse](
            items=[AuditFlowResponse.model_validate(a) for a in result.items],
            total=result.total,
            page=result.page,
            size=result.size,
            pages=result.pages,
        ),
        message="success",
    )


@router.put(
    "/{audit_id}/approve",
    response_model=ApiResponse[AuditFlowResponse],
)
async def approve_audit(
    request: Request,
    audit_id: UUID,
    body: AuditCommentBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[AuditFlowResponse]:
    """通过审核。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = AuditService(db, user, tenant_id=tenant_id)
    audit = await service.approve(audit_id, comment=body.comment)
    return ApiResponse(
        code=0,
        data=AuditFlowResponse.model_validate(audit),
        message="success",
    )


@router.put(
    "/{audit_id}/reject",
    response_model=ApiResponse[AuditFlowResponse],
)
async def reject_audit(
    request: Request,
    audit_id: UUID,
    body: AuditCommentBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[AuditFlowResponse]:
    """驳回审核。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = AuditService(db, user, tenant_id=tenant_id)
    audit = await service.reject(audit_id, comment=body.comment)
    return ApiResponse(
        code=0,
        data=AuditFlowResponse.model_validate(audit),
        message="success",
    )
