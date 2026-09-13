"""沉淀候选池 API — P3 候选池管理端点。

端点分组：
    候选列表   GET  /knowledge-candidates?status=
    驳回候选   POST /knowledge-candidates/{id}/dismiss

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑委托给 KnowledgeCandidatePool。

权限：需 admin/kb_admin 角色（候选驳回影响知识沉淀走向）。
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.services.knowledge_compounding.knowledge_candidates import (
    KnowledgeCandidatePool,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/knowledge-candidates", tags=["沉淀候选池"])


def _require_admin(user: User) -> None:
    if user.role not in ("admin", "kb_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可管理沉淀候选池",
        )


def _candidate_to_dict(row: Any) -> dict[str, Any]:
    """候选行（SQL Row / ORM 实例）→ 字典（Row 支持按列名属性访问）。"""
    def _v(key: str) -> Any:
        return getattr(row, key, None)

    return {
        "id": str(_v("id")),
        "tenant_id": str(_v("tenant_id")) if _v("tenant_id") else None,
        "asset_type": _v("asset_type"),
        "status": _v("status"),
        "representative_question": _v("representative_question"),
        "question_variants": list(_v("question_variants") or []),
        "answer_draft": _v("answer_draft"),
        "support_events": list(_v("support_events") or []),
        "support_count": int(_v("support_count") or 0),
        "praise_count": int(_v("praise_count") or 0),
        "accept_count": int(_v("accept_count") or 0),
        "distinct_users": int(_v("distinct_users") or 0),
        "first_seen_at": _iso(_v("first_seen_at")),
        "last_seen_at": _iso(_v("last_seen_at")),
        "promoted_asset_id": str(_v("promoted_asset_id")) if _v("promoted_asset_id") else None,
        "promoted_doc_id": str(_v("promoted_doc_id")) if _v("promoted_doc_id") else None,
        "dismissed_reason": _v("dismissed_reason"),
        "created_at": _iso(_v("created_at")),
        "updated_at": _iso(_v("updated_at")),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


@router.get("")
async def list_candidates(
    request: Request,
    candidate_status: str | None = Query(
        default=None, alias="status", description="状态过滤（candidate/promoting/promoted/dismissed/expired）"
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询候选池列表 — 可按状态过滤。"""
    _require_admin(user)
    tenant_id = getattr(request.state, "tenant_id", None)
    pool = KnowledgeCandidatePool(db, tenant_id=tenant_id)
    rows, total = await pool.list_candidates(
        status=candidate_status, page=page, size=size
    )
    return ApiResponse(
        code=0,
        data=PageResponse(
            items=[_candidate_to_dict(r) for r in rows],
            total=total,
            page=page,
            size=size,
            pages=math.ceil(total / size) if size else 0,
        ),
        message="success",
    )


@router.post("/{candidate_id}/dismiss")
async def dismiss_candidate(
    request: Request,
    candidate_id: uuid.UUID,
    reason: str = Query(..., description="驳回原因"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """驳回候选 — status → dismissed（留痕，不做物理删除）。

    仅 candidate 状态可驳回；promoted/expired 等终态返回 400。
    """
    _require_admin(user)
    tenant_id = getattr(request.state, "tenant_id", None)
    pool = KnowledgeCandidatePool(db, tenant_id=tenant_id)
    try:
        dismissed = await pool.dismiss(candidate_id, reason)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="驳回失败",
        ) from exc
    if not dismissed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="候选不存在或当前状态不可驳回（仅 candidate 可驳回）",
        )
    return ApiResponse(code=0, data={"id": str(candidate_id), "status": "dismissed"}, message="候选已驳回")
