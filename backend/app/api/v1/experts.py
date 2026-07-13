"""
专家发现 API — 单一职责：提供专家查找的 HTTP 端点。

端点：
    GET /experts?q=微服务            — 按关键词查找专家
    GET /experts/{user_id}/expertise — 获取用户专业领域
    GET /experts/top                  — 全站贡献排行榜
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.expert_service import ExpertService

router = APIRouter(prefix="/experts", tags=["experts"])


@router.get("")
async def find_experts(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    top_k: int = Query(5, ge=1, le=20, description="返回数量"),
    user: User = Depends(require_module("expert_discovery")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """按关键词查找相关专家 — 基于文档/问答/评论加权。"""
    service = ExpertService(db)
    experts = await service.find_experts(keyword=q, top_k=top_k)
    return ApiResponse(code=0, data=experts, message="success")


@router.get("/top")
async def get_top_contributors(
    days: int = Query(30, ge=1, le=365),
    top_k: int = Query(10, ge=1, le=50),
    user: User = Depends(require_module("expert_discovery")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """全站贡献排行榜 — 按文档数+回答数+评论数加权。

    P0 解耦：调用 ExpertService.get_top_contributors()，
    不再依赖 AnalyticsService。
    """
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = ExpertService(db)
    data = await service.get_top_contributors(days, top_k)
    return ApiResponse(code=0, data=data, message="success")


@router.get("/{user_id}/expertise")
async def get_user_expertise(
    user_id: str,
    current_user: User = Depends(require_module("expert_discovery")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取用户的专业领域。"""
    service = ExpertService(db)
    expertise = await service.get_user_expertise(user_id)
    return ApiResponse(code=0, data=expertise, message="success")
