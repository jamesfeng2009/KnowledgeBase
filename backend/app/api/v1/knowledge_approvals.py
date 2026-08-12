"""知识回流审批 API — P2 审批工作流 HTTP 端点。

端点分组：
    审批列表     GET  /knowledge-approvals
    待审批列表   GET  /knowledge-approvals/pending
    批准审批     POST /knowledge-approvals/{id}/approve
    拒绝审批     POST /knowledge-approvals/{id}/reject
    审批统计     GET  /knowledge-approvals/stats

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑委托给 KnowledgeApprovalService。

权限：approve/reject 需 admin/kb_admin 角色（审批状态流转不可逆）。
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.knowledge_approval import KnowledgeApproval
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.services.knowledge_approval_service import KnowledgeApprovalService
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/knowledge-approvals", tags=["知识回流审批"])


# ======================================================================
# 内部工具
# ======================================================================


def _require_admin(user: User) -> None:
    """要求当前用户具备管理员角色（admin/kb_admin）。

    审批状态流转不可逆，仅管理员可执行 approve/reject。
    """
    if user.role not in ("admin", "kb_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可审批知识回流内容",
        )


def _approval_to_dict(approval: KnowledgeApproval) -> dict[str, Any]:
    """将 KnowledgeApproval ORM 实例转为字典。"""
    return {
        "id": str(approval.id),
        "asset_id": str(approval.asset_id),
        "doc_id": str(approval.doc_id) if approval.doc_id else None,
        "kb_id": str(approval.kb_id),
        "reviewer_id": str(approval.reviewer_id) if approval.reviewer_id else None,
        "status": approval.status,
        "quality_score": approval.quality_score,
        "pii_detected": approval.pii_detected,
        "conflict_count": approval.conflict_count,
        "auto_detected_risks": approval.auto_detected_risks,
        "expire_at": approval.expire_at.isoformat() if approval.expire_at else None,
        "reviewed_at": approval.reviewed_at.isoformat() if approval.reviewed_at else None,
        "review_note": approval.review_note,
        "auto_approved": approval.auto_approved,
        "created_at": approval.created_at.isoformat() if approval.created_at else None,
    }


def _paginated(items: list, total: int, page: int, size: int) -> PageResponse:
    """从 (items, total) 元组构建 PageResponse。"""
    return PageResponse(
        items=items,
        total=total,
        page=page,
        size=size,
        pages=math.ceil(total / size) if size else 0,
    )


# ======================================================================
# 审批列表
# ======================================================================


@router.get("")
async def list_approvals(
    request: Request,
    approval_status: str | None = Query(
        default=None, alias="status", description="审批状态过滤"
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询审批列表 — 可按状态过滤。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeApprovalService(db, tenant_id=tenant_id)
    result = await service.list_approvals(status=approval_status, page=page, size=size)
    return ApiResponse(
        code=0,
        data=_paginated(
            [_approval_to_dict(a) for a in result.items],
            result.total,
            page,
            size,
        ),
        message="success",
    )


@router.get("/pending")
async def list_pending(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询待审批列表。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeApprovalService(db, tenant_id=tenant_id)
    result = await service.list_pending(page=page, size=size)
    return ApiResponse(
        code=0,
        data=_paginated(
            [_approval_to_dict(a) for a in result.items],
            result.total,
            page,
            size,
        ),
        message="success",
    )


@router.get("/stats")
async def get_stats(
    request: Request,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """审批统计 — 各状态计数 + 自动通过率。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeApprovalService(db, tenant_id=tenant_id)
    stats = await service.get_stats()
    return ApiResponse(code=0, data=stats, message="success")


# ======================================================================
# 审批操作
# ======================================================================


@router.post("/{approval_id}/approve")
async def approve(
    request: Request,
    approval_id: uuid.UUID,
    note: str | None = Query(default=None, description="审批备注"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """批准审批 — asset.status=active, doc.status=published。

    仅 admin/kb_admin 可操作。审批状态流转不可逆（pending → approved）。
    """
    _require_admin(user)
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeApprovalService(db, tenant_id=tenant_id)
    try:
        approval = await service.approve(approval_id, user, note=note)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return ApiResponse(
        code=0,
        data=_approval_to_dict(approval),
        message="审批已通过",
    )


@router.post("/{approval_id}/reject")
async def reject(
    request: Request,
    approval_id: uuid.UUID,
    reason: str = Query(..., description="拒绝原因"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """拒绝审批 — asset.status=deprecated, doc 软删除。

    仅 admin/kb_admin 可操作。审批状态流转不可逆（pending → rejected）。
    """
    _require_admin(user)
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeApprovalService(db, tenant_id=tenant_id)
    try:
        approval = await service.reject(approval_id, user, reason=reason)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return ApiResponse(
        code=0,
        data=_approval_to_dict(approval),
        message="审批已拒绝",
    )
