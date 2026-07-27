"""
知识库运营管理 API — 单一职责：知识缺口管理的 HTTP 端点。

端点：
    GET  /manage/knowledge-gaps            — 知识缺口列表（按搜索次数倒序）
    PUT  /manage/knowledge-gaps/{gap_id}   — 更新缺口状态（open/ignored/filled）
    POST /manage/knowledge-gaps/refresh    — 从零点击搜索词刷新缺口分析

数据流：
    搜索无结果 → SearchLog（clicked=false）→ refresh 聚合 → knowledge_gaps 表
    缺口检测与状态管理委托 GapDetectorService / KnowledgeGapRepository。

状态约定：
    前端三态 open（待处理）/ ignored（已忽略）/ filled（已填补）；
    历史数据中的 addressed 在输出时统一映射为 filled。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user, require_module
from app.models.gap import KnowledgeGap
from app.models.user import User
from app.repositories.gap_repository import KnowledgeGapRepository
from app.schemas.common import ApiResponse
from app.services.analytics_service import AnalyticsService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/manage", tags=["知识库运营管理"])

#: 前端合法状态（历史 addressed 输出时映射为 filled）
_VALID_STATUS = {"open", "ignored", "filled"}


def _gap_to_dict(gap: KnowledgeGap) -> dict:
    """将缺口模型映射为前端契约字段。

    字段映射：topic → title/topic，search_count → count，
    历史状态 addressed → filled。
    """
    status = "filled" if gap.status == "addressed" else gap.status
    return {
        "id": str(gap.id),
        "title": gap.topic,
        "topic": gap.topic,
        "description": gap.description,
        "priority": gap.priority,
        "status": status,
        "count": gap.search_count,
        "suggestion": gap.suggestion,
        "created_at": gap.created_at.isoformat() if gap.created_at else None,
        "updated_at": gap.updated_at.isoformat() if gap.updated_at else None,
    }


@router.get("/knowledge-gaps")
async def list_knowledge_gaps(
    request: Request,
    priority: str | None = Query(None, description="优先级过滤: high/medium/low"),
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """知识缺口列表 — 按无结果搜索次数倒序。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    tenant_id = getattr(request.state, "tenant_id", None)
    repo = KnowledgeGapRepository(db, tenant_id=tenant_id)
    gaps = await repo.get_all(priority=priority)
    return ApiResponse(
        code=0,
        data=[_gap_to_dict(g) for g in gaps],
        message="success",
    )


@router.put("/knowledge-gaps/{gap_id}")
async def update_knowledge_gap_status(
    request: Request,
    gap_id: UUID,
    body: dict = Body(...),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """更新缺口状态：open（待处理）/ ignored（已忽略）/ filled（已填补）。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    status = str(body.get("status") or "").strip()
    if status not in _VALID_STATUS:
        return ApiResponse(
            code=400,
            data=None,
            message=f"非法状态: {status}，允许值: {sorted(_VALID_STATUS)}",
        )

    tenant_id = getattr(request.state, "tenant_id", None)
    repo = KnowledgeGapRepository(db, tenant_id=tenant_id)
    gap = await repo.update_status(gap_id, status=status)
    if gap is None:
        return ApiResponse(code=404, data=None, message="缺口不存在")

    log.info("knowledge_gap.status_updated", gap_id=str(gap_id), status=status)
    return ApiResponse(code=0, data=_gap_to_dict(gap), message="状态已更新")


@router.post("/knowledge-gaps/refresh")
async def refresh_knowledge_gaps(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="统计周期（天）"),
    top_k: int = Query(50, ge=1, le=200, description="纳入分析的零点击词数量"),
    user: User = Depends(require_module("analytics_dashboard")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """刷新缺口分析 — 从近 N 天零点击搜索词聚合生成/累加缺口。

    幂等：相同主题已存在时递增 search_count 并按阈值调整优先级，
    不产生重复记录。
    """
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    tenant_id = getattr(request.state, "tenant_id", None)
    analytics = AnalyticsService(db, tenant_id=tenant_id)
    zero_click = await analytics.get_zero_click_queries(days=days, top_k=top_k)

    repo = KnowledgeGapRepository(db, tenant_id=tenant_id)
    refreshed = 0
    for item in zero_click:
        keyword = (item.get("keyword") or "").strip()
        if not keyword:
            continue
        await repo.increment_search_count(keyword)
        refreshed += 1

    log.info(
        "knowledge_gaps.refreshed",
        days=days,
        zero_click_terms=len(zero_click),
        refreshed=refreshed,
    )
    return ApiResponse(
        code=0,
        data={"refreshed": refreshed, "zero_click_terms": len(zero_click)},
        message=f"已基于近 {days} 天零点击搜索词刷新 {refreshed} 条缺口",
    )
