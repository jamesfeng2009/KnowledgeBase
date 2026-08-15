"""约束规则人审 API — P2 写入打标闭环（GAP-3）。

仿 observability.py 的高风险审计模式（管理员校验 + ApiResponse +
服务层单例延迟导入）。端点：
    GET  /constraints/rules            — 人审队列（默认 pending_review）
    POST /constraints/rules/{id}/review — approve → active / reject → retired
    GET  /constraints/review-stats     — 误判率统计（反哺置信度阈值）
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse

router = APIRouter(tags=["约束管理"])


def _require_admin(user: User) -> None:
    """校验当前用户是否为管理员。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可管理约束规则",
        )


class ConstraintRuleListResponse(BaseModel):
    """约束规则列表响应。"""

    total: int
    items: list[dict[str, Any]]


class ConstraintReviewRequest(BaseModel):
    """人审请求体。"""

    action: str = Field(..., pattern="^(approve|reject)$")
    comment: str | None = None


class ConstraintReviewStats(BaseModel):
    """误判率统计响应 — 反哺置信度阈值。"""

    total_rules: int
    pending_review: int
    active: int
    retired: int
    reviewed: int
    misjudged: int
    misjudgment_rate: float | None = None
    auto_high_confidence_rejected: int
    version_chain_retired: int
    current_thresholds: dict[str, Any]


@router.get(
    "/constraints/rules",
    response_model=ApiResponse[ConstraintRuleListResponse],
)
async def list_constraint_rules(
    status_filter: str | None = Query(
        "pending_review",
        alias="status",
        description="按状态筛选: active/pending_review/retired（空=全部）",
    ),
    kb_id: str | None = Query(None, description="按知识库筛选"),
    severity: str | None = Query(None, description="按 severity 筛选: block/confirm/warn"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[ConstraintRuleListResponse]:
    """查询约束规则（P2 人审队列）。

    权限：仅管理员。默认拉 pending_review 待审队列。
    """
    _require_admin(user)

    from app.services.constraint_review_service import (
        get_constraint_review_service,
    )

    kb_uuid: UUID | None = None
    if kb_id:
        try:
            kb_uuid = UUID(kb_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的知识库 ID",
            )

    result = await get_constraint_review_service().list_rules(
        status_filter=status_filter,
        kb_id=kb_uuid,
        severity=severity,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return ApiResponse(
        code=0,
        data=ConstraintRuleListResponse(
            total=result["total"], items=result["items"]
        ),
    )


@router.post(
    "/constraints/rules/{rule_id}/review",
    response_model=ApiResponse[dict[str, Any]],
)
async def review_constraint_rule(
    rule_id: str,
    body: ConstraintReviewRequest,
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict[str, Any]]:
    """人审约束规则（P2）— approve 转 active / reject 转 retired。

    权限：仅管理员。reject 计入误判统计，反哺
    CONSTRAINT_AUTO_CONFIDENCE 阈值。
    """
    _require_admin(user)

    from app.services.constraint_review_service import (
        get_constraint_review_service,
    )

    try:
        rule_uuid = UUID(rule_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的规则 ID",
        )

    try:
        updated = await get_constraint_review_service().review_rule(
            rule_uuid,
            action=body.action,
            reviewer_id=user.id,
            comment=body.comment,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="约束规则不存在",
        )
    return ApiResponse(
        code=0,
        data={"id": rule_id, "status": "active" if body.action == "approve" else "retired"},
    )


@router.get(
    "/constraints/review-stats",
    response_model=ApiResponse[ConstraintReviewStats],
)
async def get_constraint_review_stats(
    user: User = Depends(get_current_active_user),
) -> ApiResponse[ConstraintReviewStats]:
    """约束打标误判率统计（P2）— 反哺置信度阈值。

    权限：仅管理员。误判率 = 人审 reject 数 / 已人审数；
    auto_high_confidence_rejected 是 CONSTRAINT_AUTO_CONFIDENCE 偏低的直接证据。
    """
    _require_admin(user)

    from app.services.constraint_review_service import (
        get_constraint_review_service,
    )

    stats = await get_constraint_review_service().get_review_stats()
    return ApiResponse(
        code=0,
        data=ConstraintReviewStats(**stats),
    )
