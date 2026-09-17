"""
任务队列监控 API — 单一职责：暴露 Celery/RabbitMQ 队列面板数据。

路由：
    GET /observability/queues — 队列深度 + outbox 失败重试统计（管理员）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.queue_monitor_service import QueueMonitorService

router = APIRouter(prefix="/observability", tags=["可观测性"])


@router.get("/queues", response_model=ApiResponse[dict])
async def queue_overview(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """任务队列面板：RabbitMQ 队列深度 + Outbox 失败重试统计。"""
    if user.role not in ("admin", "ops"):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="仅管理员可查看队列面板")
    service = QueueMonitorService(db)
    return ApiResponse(
        code=0,
        data=await service.overview(),
        message="success",
    )
