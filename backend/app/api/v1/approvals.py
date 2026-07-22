"""
工具审批路由 — 单一职责：审批记录的查询与操作 REST 端点。

P1 核心：前端通过这些端点查询待审批列表、批准或拒绝危险工具调用。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.approval import (
    ApprovalActionRequest,
    ToolApprovalResponse,
)
from app.services.approval_service import ApprovalService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["工具审批"])


@router.get("/pending")
async def list_pending_approvals(
    request: Request,
    session_id: str | None = None,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> dict:
    """查询当前用户的待审批列表。

    Args:
        session_id: 可选，按会话过滤。

    Returns:
        {"pending": [ToolApprovalResponse], "total": int}
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ApprovalService(db, tenant_id=tenant_id)
    approvals = await service.get_pending_approvals(user, session_id)
    return {
        "pending": [
            ToolApprovalResponse.model_validate(a).model_dump() for a in approvals
        ],
        "total": len(approvals),
    }


@router.post("/{approval_id}/approve")
async def approve_tool(
    request: Request,
    approval_id: UUID,
    _body: ApprovalActionRequest | None = None,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> dict:
    """批准工具执行 — 用户同意执行危险工具。

    批准后该工具在当前会话内不再需要重复审批。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ApprovalService(db, tenant_id=tenant_id)
    try:
        approval = await service.approve(approval_id, user)
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "code": 0,
        "data": ToolApprovalResponse.model_validate(approval).model_dump(),
        "message": "已批准执行",
    }


@router.post("/{approval_id}/reject")
async def reject_tool(
    request: Request,
    approval_id: UUID,
    _body: ApprovalActionRequest | None = None,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> dict:
    """拒绝工具执行 — 用户拒绝执行危险工具。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ApprovalService(db, tenant_id=tenant_id)
    try:
        approval = await service.reject(approval_id, user)
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "code": 0,
        "data": ToolApprovalResponse.model_validate(approval).model_dump(),
        "message": "已拒绝执行",
    }


@router.get("/{approval_id}")
async def get_approval(
    request: Request,
    approval_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> dict:
    """查询单个审批记录详情。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ApprovalService(db, tenant_id=tenant_id)
    approval = await service.get_approval_by_id(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    if approval.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权查看此审批记录")
    return {
        "code": 0,
        "data": ToolApprovalResponse.model_validate(approval).model_dump(),
    }
