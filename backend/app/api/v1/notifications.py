"""
通知中心 API — 单一职责：提供知识推送通知的 HTTP 端点。

端点：
    GET  /notifications              — 获取通知列表
    GET  /notifications/unread-count   — 获取未读数量
    GET  /notifications/stream        — SSE 实时推送流
    PUT  /notifications/{id}/read      — 标记单条已读
    PUT  /notifications/read-all       — 标记全部已读
    POST /notifications/trigger-digest — 手动触发日报（admin）
    POST /notifications/trigger-gap    — 手动触发缺口预警（admin）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.notification_hub import subscribe_stream
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def get_notifications(
    unread_only: bool = Query(False, description="仅返回未读"),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_module("knowledge_push")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取当前用户的通知列表。"""
    service = NotificationService(db)
    notifications = await service.get_user_notifications(
        user_id=str(user.id),
        unread_only=unread_only,
        limit=limit,
    )
    return ApiResponse(code=0, data=notifications, message="success")


@router.get("/unread-count")
async def get_unread_count(
    user: User = Depends(require_module("knowledge_push")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取未读通知数量。"""
    service = NotificationService(db)
    notifications = await service.get_user_notifications(
        user_id=str(user.id),
        unread_only=True,
        limit=100,
    )
    return ApiResponse(code=0, data={"count": len(notifications)}, message="success")


@router.get("/stream")
async def notification_stream(
    user: User = Depends(require_module("knowledge_push")),
) -> StreamingResponse:
    """SSE 实时通知推送流。

    前端使用 EventSource 连接本端点，通知写入数据库后通过 Redis Pub/Sub
    实时推送到此端点。每 30 秒发送心跳保活，断线后浏览器自动重连。
    """
    return StreamingResponse(
        subscribe_stream(user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.put("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    user: User = Depends(require_module("knowledge_push")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """标记单条通知为已读。"""
    service = NotificationService(db)
    success = await service.mark_as_read(notification_id)
    if not success:
        return ApiResponse(code=404, data=None, message="通知不存在")
    await db.commit()
    return ApiResponse(code=0, data={"read": True}, message="success")


@router.put("/read-all")
async def mark_all_read(
    user: User = Depends(require_module("knowledge_push")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """标记当前用户所有通知为已读。"""
    service = NotificationService(db)
    count = await service.mark_all_read(str(user.id))
    await db.commit()
    return ApiResponse(code=0, data={"updated": count}, message="success")


@router.post("/trigger-digest")
async def trigger_digest(
    user: User = Depends(require_module("knowledge_push")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """手动触发当前用户的知识日报 — 即时生成。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = NotificationService(db)
    recommendations = await service.generate_personal_digest(str(user.id))
    await db.commit()
    return ApiResponse(
        code=0,
        data={"recommendations": recommendations},
        message="success",
    )


@router.post("/trigger-gap-alert")
async def trigger_gap_alert(
    user: User = Depends(require_module("knowledge_push")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """手动触发知识缺口预警 — 即时通知管理员。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    service = NotificationService(db)
    notified = await service.send_gap_alert()
    await db.commit()
    return ApiResponse(
        code=0,
        data={"notified": notified},
        message="success",
    )
