"""
报表路由 — 单一职责：处理使用量、知识库与成本报表的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
数据聚合查询委托给 ReportRepository。

权限策略：
- 使用量报表与知识库报表：所有已认证用户可查看；
- 成本报表：仅 admin 可查看（涉及费用敏感信息）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.repositories.report_repository import ReportRepository
from app.schemas.common import ApiResponse
from app.schemas.report import (
    CostReport,
    CostReportSeries,
    GroupBy,
    KnowledgeReport,
    ReportFilter,
    UsageReport,
    UsageReportSeries,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["报表"])

# 响应时间估算基准（无实际数据时的回退值）
_AVG_RESPONSE_TIME_FALLBACK = 1.5
# 平均质量分估算基准（质量分尚未持久化到 UsageRecord，暂用估算）
_AVG_QUALITY_SCORE_ESTIMATE = 75.0


def _ms_to_seconds(ms: float | int | None) -> float:
    """毫秒转秒，None/0 时回退到估算基准值。"""
    if ms and float(ms) > 0:
        return round(float(ms) / 1000.0, 2)
    return _AVG_RESPONSE_TIME_FALLBACK


def _require_admin(user: User) -> None:
    """校验当前用户是否为管理员。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可查看成本报表",
        )


def _to_datetime(d: date, end_of_day: bool = False) -> datetime:
    """将 date 转为带时区的 datetime。

    Args:
        d: 日期对象。
        end_of_day: 是否取当天末尾（23:59:59）。

    Returns:
        带时区的 datetime。
    """
    if end_of_day:
        return datetime.combine(d, datetime.max.time(), tzinfo=timezone.utc)
    return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)


# ======================================================================
# 使用量报表
# ======================================================================


@router.get("/reports/usage", response_model=ApiResponse[UsageReportSeries])
async def get_usage_report(
    start_date: date = Query(..., description="起始日期"),
    end_date: date = Query(..., description="结束日期"),
    group_by: GroupBy = Query(default=GroupBy.day, description="分组维度"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[UsageReportSeries]:
    """获取使用量报表（按时间分组）。

    指标：总查询次数、独立用户数、平均响应时间、总 token、成本。
    """
    report_filter = ReportFilter(
        start_date=start_date,
        end_date=end_date,
        group_by=group_by,
    )

    repo = ReportRepository(db)
    start_dt = _to_datetime(start_date)
    end_dt = _to_datetime(end_date, end_of_day=True)

    # 汇总统计
    summary_raw = await repo.get_usage_stats(start_dt, end_dt)

    # 时间序列
    series_raw = await repo.get_query_logs(start_dt, end_dt, group_by.value)

    items = [
        UsageReport(
            period=item["period"],
            total_queries=item["total_queries"],
            unique_users=item["unique_users"],
            avg_response_time=_ms_to_seconds(item.get("avg_duration_ms")),
            total_tokens=item["total_tokens"],
            cost=float(item["total_cost_cents"]) / 100.0,
        )
        for item in series_raw
    ]

    summary = UsageReport(
        period=f"{start_date} ~ {end_date}",
        total_queries=summary_raw["total_queries"],
        unique_users=summary_raw["unique_users"],
        avg_response_time=_ms_to_seconds(summary_raw.get("avg_duration_ms")),
        total_tokens=summary_raw["total_tokens"],
        cost=float(summary_raw["total_cost_cents"]) / 100.0,
    )

    return ApiResponse(
        code=0,
        data=UsageReportSeries(
            filter=report_filter,
            items=items,
            summary=summary,
        ),
        message="success",
    )


# ======================================================================
# 知识库报表
# ======================================================================


@router.get("/reports/knowledge", response_model=ApiResponse[KnowledgeReport])
async def get_knowledge_report(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[KnowledgeReport]:
    """获取知识库全局统计报表。

    指标：文档总数、知识库总数、平均质量分、知识缺口数、即将过期文档数。
    """
    repo = ReportRepository(db)
    stats = await repo.get_knowledge_stats()

    return ApiResponse(
        code=0,
        data=KnowledgeReport(
            total_docs=stats["total_docs"],
            total_kbs=stats["total_kbs"],
            avg_quality_score=_AVG_QUALITY_SCORE_ESTIMATE,
            gap_count=0,  # 知识缺口需要额外的知识图谱分析
            expiring_count=stats.get("archived_count", 0),
        ),
        message="success",
    )


# ======================================================================
# 成本报表
# ======================================================================


@router.get("/reports/cost", response_model=ApiResponse[CostReportSeries])
async def get_cost_report(
    start_date: date = Query(..., description="起始日期"),
    end_date: date = Query(..., description="结束日期"),
    group_by: GroupBy = Query(default=GroupBy.day, description="分组维度"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[CostReportSeries]:
    """获取成本报表（仅 admin 权限）。

    按模型和请求类型分组，返回成本明细。
    """
    _require_admin(user)

    report_filter = ReportFilter(
        start_date=start_date,
        end_date=end_date,
        group_by=group_by,
    )

    repo = ReportRepository(db)
    start_dt = _to_datetime(start_date)
    end_dt = _to_datetime(end_date, end_of_day=True)

    # 成本汇总
    cost_raw = await repo.get_cost_stats(start_dt, end_dt)

    # 时间序列（复用用量查询的时间分组）
    series_raw = await repo.get_query_logs(start_dt, end_dt, group_by.value)

    items = [
        CostReport(
            period=item["period"],
            total_cost=float(item["total_cost_cents"]) / 100.0,
            total_input_tokens=0,  # 时间序列中不拆分 input/output
            total_output_tokens=0,
            by_model={},
            by_request_type={},
        )
        for item in series_raw
    ]

    summary = CostReport(
        period=f"{start_date} ~ {end_date}",
        total_cost=float(cost_raw["total_cost_cents"]) / 100.0,
        total_input_tokens=cost_raw["total_input_tokens"],
        total_output_tokens=cost_raw["total_output_tokens"],
        by_model=cost_raw["by_model"],
        by_request_type=cost_raw["by_request_type"],
    )

    return ApiResponse(
        code=0,
        data=CostReportSeries(
            filter=report_filter,
            items=items,
            summary=summary,
        ),
        message="success",
    )
