"""
知识健康度仪表盘 API — 单一职责：提供运营指标数据的 HTTP 端点。

端点：
    GET /analytics/dashboard          — 仪表盘汇总（一次返回所有指标）
    GET /analytics/search-hotwords     — 搜索热词 Top N
    GET /analytics/zero-click          — 零点击搜索词
    GET /analytics/popular-docs        — 文档热度排行
    GET /analytics/coverage            — 知识覆盖率
    GET /analytics/freshness           — 知识新鲜度
    GET /analytics/contributors        — 专家贡献排行
    POST /analytics/log-search         — 记录搜索行为（内部调用）
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user, require_module
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/dashboard")
async def get_dashboard(
    days: int = Query(30, ge=1, le=365, description="统计周期（天）"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """仪表盘汇总 — 一次返回所有六项指标。

    所有登录用户可查看；空数据时返回默认值。
    """
    service = AnalyticsService(db)
    data = await service.get_dashboard(days)
    return ApiResponse(code=0, data=data, message="success")


@router.get("/search-hotwords")
async def get_search_hotwords(
    days: int = Query(30, ge=1, le=365),
    top_k: int = Query(20, ge=1, le=100),
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """搜索热词 Top N — 按搜索次数降序。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = AnalyticsService(db)
    data = await service.get_search_hotwords(days, top_k)
    return ApiResponse(code=0, data=data, message="success")


@router.get("/zero-click")
async def get_zero_click_queries(
    days: int = Query(30, ge=1, le=365),
    top_k: int = Query(20, ge=1, le=100),
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """零点击搜索词 — 无结果或用户未点击的查询。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = AnalyticsService(db)
    data = await service.get_zero_click_queries(days, top_k)
    return ApiResponse(code=0, data=data, message="success")


@router.get("/popular-docs")
async def get_popular_documents(
    days: int = Query(30, ge=1, le=365),
    top_k: int = Query(10, ge=1, le=50),
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """文档热度排行 — 按浏览量降序。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = AnalyticsService(db)
    data = await service.get_popular_documents(days, top_k)
    return ApiResponse(code=0, data=data, message="success")


@router.get("/coverage")
async def get_knowledge_coverage(
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """知识覆盖率 — 已覆盖主题 / 搜索主题比例。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = AnalyticsService(db)
    data = await service.get_knowledge_coverage()
    return ApiResponse(code=0, data=data, message="success")


@router.get("/freshness")
async def get_knowledge_freshness(
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """知识新鲜度 — 过期/即将过期文档比例。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = AnalyticsService(db)
    data = await service.get_knowledge_freshness()
    return ApiResponse(code=0, data=data, message="success")


@router.get("/contributors")
async def get_top_contributors(
    days: int = Query(30, ge=1, le=365),
    top_k: int = Query(10, ge=1, le=50),
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """专家贡献排行 — 按文档数+回答数+评论数加权。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = AnalyticsService(db)
    data = await service.get_top_contributors(days, top_k)
    return ApiResponse(code=0, data=data, message="success")


@router.post("/log-search")
async def log_search(
    query: str = Body(..., embed=True),
    result_count: int = Body(0, embed=True),
    source: str = Body("knowledge_base", embed=True),
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """记录搜索行为 — 供仪表盘统计。

    前端每次搜索时调用此端点记录行为日志。
    """
    service = AnalyticsService(db)
    await service.log_search(
        query=query,
        user_id=str(user.id),
        source=source,
        result_count=result_count,
    )
    await db.commit()
    return ApiResponse(code=0, data={"logged": True}, message="success")
