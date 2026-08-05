"""可观测性 API — 提供 LLM 调用链路的用量、耗时和成功率数据。

P0-Stage4: 前端可观测面板的数据源，基于 UsageRecord 表的真实数据
（替代 reports.py 中的硬编码估算值）。

端点：
    GET /observability/recent  — 最近 LLM 调用记录（分页）
    GET /observability/stats   — 聚合统计（总量、成本、平均耗时、成功率）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.billing import UsageRecord
from app.models.user import User
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["可观测性"])


def _require_admin(user: User) -> None:
    """校验当前用户是否为管理员。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可查看可观测性数据",
        )


# ------------------------------------------------------------------
# Response Schemas
# ------------------------------------------------------------------


class UsageRecordItem(BaseModel):
    """单条用量记录。"""

    id: str
    created_at: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_cents: int
    request_type: str
    duration_ms: int
    success: bool
    request_id: str | None = None
    user_id: str | None = None


class RecentUsageResponse(BaseModel):
    """最近用量记录响应。"""

    items: list[UsageRecordItem]
    total: int
    page: int
    page_size: int


class ObservabilityStats(BaseModel):
    """可观测性聚合统计。"""

    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_cents: int
    avg_duration_ms: float
    success_rate: float
    # 按模型分组的用量
    by_model: list[dict[str, Any]]
    # 按日期分组的请求量
    by_date: list[dict[str, Any]]


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.get("/observability/recent", response_model=ApiResponse[RecentUsageResponse])
async def get_recent_usage(
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    model: str | None = Query(None, description="按模型筛选"),
    success: bool | None = Query(None, description="按成功/失败筛选"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[RecentUsageResponse]:
    """获取最近的 LLM 调用记录（分页）。

    权限：仅管理员。
    数据源：UsageRecord 表（P0-Stage2 起写入真实 token 用量和耗时）。
    """
    _require_admin(user)

    # 构建查询条件
    conditions = []
    if model:
        conditions.append(UsageRecord.model.ilike(f"%{model}%"))
    if success is not None:
        conditions.append(UsageRecord.success == success)

    # 总数查询
    count_stmt = select(func.count(UsageRecord.id))
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = (await db.execute(count_stmt)).scalar() or 0

    # 分页查询
    stmt = (
        select(UsageRecord)
        .order_by(desc(UsageRecord.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    for cond in conditions:
        stmt = stmt.where(cond)

    rows = (await db.execute(stmt)).scalars().all()

    items = [
        UsageRecordItem(
            id=str(r.id),
            created_at=r.created_at.isoformat() if r.created_at else "",
            model=r.model,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cost_cents=r.cost_cents,
            request_type=r.request_type,
            duration_ms=r.duration_ms,
            success=r.success,
            request_id=r.request_id,
            user_id=str(r.user_id) if r.user_id else None,
        )
        for r in rows
    ]

    return ApiResponse(
        code=0,
        data=RecentUsageResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        ),
    )


@router.get("/observability/stats", response_model=ApiResponse[ObservabilityStats])
async def get_observability_stats(
    days: int = Query(7, ge=1, le=90, description="统计天数（最近 N 天）"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[ObservabilityStats]:
    """获取可观测性聚合统计。

    权限：仅管理员。
    返回：总请求数、总 token、总成本、平均耗时、成功率，
         以及按模型和按日期分组的统计。
    """
    _require_admin(user)

    start_date = datetime.now(timezone.utc) - timedelta(days=days)

    # 总体统计
    from sqlalchemy import case

    overall_stmt = (
        select(
            func.count(UsageRecord.id).label("total_requests"),
            func.sum(UsageRecord.input_tokens).label("total_input"),
            func.sum(UsageRecord.output_tokens).label("total_output"),
            func.sum(UsageRecord.cost_cents).label("total_cost"),
            func.avg(UsageRecord.duration_ms).label("avg_duration"),
            func.sum(
                case((UsageRecord.success == True, 1), else_=0)  # noqa: E712
            ).label("success_count"),
        )
        .where(UsageRecord.created_at >= start_date)
    )
    row = (await db.execute(overall_stmt)).one()

    total_requests = int(row.total_requests or 0)
    success_count = int(row.success_count or 0)

    # 按模型分组
    model_stmt = (
        select(
            UsageRecord.model,
            func.count(UsageRecord.id).label("count"),
            func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens).label(
                "tokens"
            ),
            func.sum(UsageRecord.cost_cents).label("cost"),
            func.avg(UsageRecord.duration_ms).label("avg_duration"),
        )
        .where(UsageRecord.created_at >= start_date)
        .group_by(UsageRecord.model)
        .order_by(desc("tokens"))
    )
    model_rows = (await db.execute(model_stmt)).all()
    by_model = [
        {
            "model": r.model,
            "count": int(r.count or 0),
            "tokens": int(r.tokens or 0),
            "cost_cents": int(r.cost or 0),
            "avg_duration_ms": round(float(r.avg_duration or 0), 2),
        }
        for r in model_rows
    ]

    # 按日期分组
    date_stmt = (
        select(
            func.date_trunc("day", UsageRecord.created_at).label("date"),
            func.count(UsageRecord.id).label("count"),
            func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens).label(
                "tokens"
            ),
            func.sum(UsageRecord.cost_cents).label("cost"),
        )
        .where(UsageRecord.created_at >= start_date)
        .group_by("date")
        .order_by("date")
    )
    date_rows = (await db.execute(date_stmt)).all()
    by_date = [
        {
            "date": str(r.date),
            "count": int(r.count or 0),
            "tokens": int(r.tokens or 0),
            "cost_cents": int(r.cost or 0),
        }
        for r in date_rows
    ]

    return ApiResponse(
        code=0,
        data=ObservabilityStats(
            total_requests=total_requests,
            total_input_tokens=int(row.total_input or 0),
            total_output_tokens=int(row.total_output or 0),
            total_cost_cents=int(row.total_cost or 0),
            avg_duration_ms=round(float(row.avg_duration or 0), 2),
            success_rate=round(success_count / total_requests * 100, 1)
            if total_requests > 0
            else 0.0,
            by_model=by_model,
            by_date=by_date,
        ),
    )


# ------------------------------------------------------------------
# P0-3: Agent 执行轨迹聚合指标
# ------------------------------------------------------------------


class TrailWindowMetrics(BaseModel):
    """Agent Loop 轨迹窗口指标。"""

    total_sessions: int
    completion_rate: float
    max_iterations_rate: float
    timeout_rate: float
    dedup_hit_rate: float
    loitering_rate: float  # 兜圈率 = dedup 指针会话占比 + max_iterations 会话占比
    tool_call_rate: float
    tool_hit_rate: float
    avg_iterations: float
    avg_think_latency_ms: float
    avg_retrieve_latency_ms: float
    avg_tool_latency_ms: float


class TrailSummaryResponse(BaseModel):
    """轨迹聚合响应。"""

    window: TrailWindowMetrics
    tool_distribution: dict[str, int]


@router.get(
    "/observability/trajectory",
    response_model=ApiResponse[TrailSummaryResponse],
)
async def get_trajectory_metrics(
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TrailSummaryResponse]:
    """获取 Agent 执行轨迹聚合指标（P0-3）。

    权限：仅管理员。
    返回：完成率 / 工具命中率 / 平均收敛步数 / 兜圈率 / 超时率，
         以及工具命中分布。卡死监控告警可通过
         ``max_iterations_rate + timeout_rate`` 阈值规则驱动。
    """
    _require_admin(user)

    from app.observability.trail_aggregator import get_trail_aggregator

    summary = await get_trail_aggregator().window_summary()
    return ApiResponse(
        code=0,
        data=TrailSummaryResponse(
            window=TrailWindowMetrics(**summary["window"]),
            tool_distribution=summary["tool_distribution"],
        ),
    )


# ------------------------------------------------------------------
# P1-8: 高风险拦截审计 — block 决策复查与误判率统计
# ------------------------------------------------------------------


class HighRiskAuditListResponse(BaseModel):
    """高风险审计记录列表响应。"""

    total: int
    items: list[dict[str, Any]]


class HighRiskReviewRequest(BaseModel):
    """复查请求体。"""

    review_status: str = Field(..., pattern="^(confirmed|misjudged)$")
    comment: str | None = None


class HighRiskMisjudgmentStats(BaseModel):
    """误判率统计响应 — 反哺三档分级阈值。"""

    total_blocks: int
    pending: int
    confirmed: int
    misjudged: int
    misjudgment_rate: float | None = None
    current_thresholds: dict[str, Any]


@router.get(
    "/observability/high-risk-audits",
    response_model=ApiResponse[HighRiskAuditListResponse],
)
async def list_high_risk_audits(
    review_status: str | None = Query(
        None, description="按复查状态筛选: pending/confirmed/misjudged"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[HighRiskAuditListResponse]:
    """查询高风险拦截审计记录（P1-8）。

    权限：仅管理员。
    """
    _require_admin(user)

    from app.services.high_risk_audit_service import get_high_risk_audit_service

    result = await get_high_risk_audit_service().list_audits(
        review_status=review_status,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return ApiResponse(
        code=0,
        data=HighRiskAuditListResponse(
            total=result["total"], items=result["items"]
        ),
    )


@router.post(
    "/observability/high-risk-audits/{record_id}/review",
    response_model=ApiResponse[dict[str, Any]],
)
async def review_high_risk_audit(
    record_id: str,
    body: HighRiskReviewRequest,
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict[str, Any]]:
    """复查高风险拦截决策（P1-8）— 标记 confirmed / misjudged。

    权限：仅管理员。误判记录累积用于定期评估阈值合理性。
    """
    _require_admin(user)

    import uuid as _uuid

    from app.services.high_risk_audit_service import get_high_risk_audit_service

    try:
        record_uuid = _uuid.UUID(record_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的审计记录 ID",
        )

    updated = await get_high_risk_audit_service().review_audit(
        record_uuid,
        review_status=body.review_status,
        reviewer_id=user.id,
        comment=body.comment,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="审计记录不存在",
        )
    return ApiResponse(code=0, data={"id": record_id, "review_status": body.review_status})


@router.get(
    "/observability/high-risk-audits/stats",
    response_model=ApiResponse[HighRiskMisjudgmentStats],
)
async def get_high_risk_misjudgment_stats(
    user: User = Depends(get_current_active_user),
) -> ApiResponse[HighRiskMisjudgmentStats]:
    """高风险拦截误判率统计（P1-8）— 反哺三档分级阈值。

    权限：仅管理员。误判率 = misjudged / (confirmed + misjudged)。
    """
    _require_admin(user)

    from app.services.high_risk_audit_service import get_high_risk_audit_service

    stats = await get_high_risk_audit_service().get_misjudgment_stats()
    return ApiResponse(
        code=0,
        data=HighRiskMisjudgmentStats(
            total_blocks=stats["total_blocks"],
            pending=stats["pending"],
            confirmed=stats["confirmed"],
            misjudged=stats["misjudged"],
            misjudgment_rate=stats["misjudgment_rate"],
            current_thresholds=stats["current_thresholds"],
        ),
    )
