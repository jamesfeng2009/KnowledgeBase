"""
计费路由 — 单一职责：处理套餐、订阅与用量聚合的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
计费领域逻辑（配额强制、订阅生命周期、用量聚合）委托给 BillingService。

权限策略：
- 套餐列表 / 当前订阅 / 用量聚合：所有已认证用户可查看（自身租户）；
- 套餐切换 / 取消 / 恢复：仅 admin（手动开通过渡期，管理员操作）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.billing import (
    PlanInfo,
    PlanStatus,
    SubscriptionResponse,
    UsageAggregate,
)
from app.schemas.common import ApiResponse
from app.services.billing_service import BillingService
from app.services.plans import PLANS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["计费"])


def _require_admin(user: User) -> None:
    """校验当前用户是否为管理员。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可执行该操作",
        )


def _current_tenant_id(request: Request) -> str | None:
    """获取当前请求绑定的租户 ID（由租户上下文中间件注入）。"""
    return getattr(request.state, "tenant_id", None)


@router.get("/plans", response_model=ApiResponse[list[PlanInfo]])
async def list_plans(
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[PlanInfo]]:
    """获取所有可用套餐及配额信息。"""
    plans = [
        PlanInfo(
            id=pid,
            name=cfg["name"],
            max_users=cfg["max_users"],
            max_storage_bytes=cfg["max_storage_bytes"],
            max_llm_tokens_per_month=cfg["max_llm_tokens_per_month"],
            price_cents=cfg["price_cents"],
        )
        for pid, cfg in PLANS.items()
    ]
    return ApiResponse(code=0, data=plans, message="success")


@router.get("/subscription", response_model=ApiResponse[dict])
async def get_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """获取当前租户的订阅与计划状态。"""
    tenant_id = _current_tenant_id(request)
    billing = BillingService(db)
    sub = await billing.get_subscription(user.tenant_id)
    plan_status = await billing.get_plan_status(user.tenant_id)

    data = {
        "plan_status": PlanStatus(**plan_status).model_dump(),
        "subscription": (
            SubscriptionResponse.model_validate(sub).model_dump() if sub else None
        ),
    }
    return ApiResponse(code=0, data=data, message="success")


@router.post("/subscription/switch", response_model=ApiResponse[SubscriptionResponse])
async def switch_subscription(
    request: Request,
    plan: str,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[SubscriptionResponse]:
    """切换套餐（手动开通过渡 — 管理员操作，立即生效）。

    Args:
        plan: 目标套餐（free/pro/enterprise）。

    业务异常：
    - 非 admin → 403
    - 套餐 ID 无效 / 租户不存在 → 400
    """
    _require_admin(user)
    billing = BillingService(db)
    try:
        sub = await billing.switch_plan(user.tenant_id, plan, manual=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    await db.commit()
    return ApiResponse(
        code=0,
        data=SubscriptionResponse.model_validate(sub),
        message="success",
    )


@router.post("/subscription/cancel", response_model=ApiResponse[dict])
async def cancel_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """取消订阅（管理员操作）— 到期/停服。"""
    _require_admin(user)
    billing = BillingService(db)
    sub = await billing.cancel_subscription(user.tenant_id)
    await db.commit()
    return ApiResponse(
        code=0,
        data={
            "cancelled": sub is not None,
            "plan_status": PlanStatus(
                **await billing.get_plan_status(user.tenant_id)
            ).model_dump(),
        },
        message="success",
    )


@router.post("/subscription/reactivate", response_model=ApiResponse[SubscriptionResponse])
async def reactivate_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[SubscriptionResponse]:
    """恢复订阅（管理员操作）— 欠费/到期后续费开通，回到免费套餐。"""
    _require_admin(user)
    billing = BillingService(db)
    try:
        sub = await billing.reactivate_subscription(user.tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    await db.commit()
    return ApiResponse(
        code=0,
        data=SubscriptionResponse.model_validate(sub),
        message="success",
    )


@router.get("/usage", response_model=ApiResponse[UsageAggregate])
async def get_usage_aggregate(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[UsageAggregate]:
    """获取当前租户当月用量与账单聚合（P0-4）。

    指标：LLM token 用量与配额、成本估算、用户数、存储用量。
    """
    billing = BillingService(db)
    aggregate = await billing.get_usage_aggregate(user.tenant_id)
    return ApiResponse(
        code=0,
        data=UsageAggregate(**aggregate),
        message="success",
    )
