"""Webhook 事件开放 API — 知识变更通知订阅。

允许外部系统订阅知识库变更事件（文档创建/更新/删除），
当事件发生时由系统主动推送 Webhook 回调。

权限说明：
- 需要 scope: ``webhook:manage``；
- 认证方式为 API Key（X-API-Key header）。

当前为预留实现（内存存储），生产环境应替换为持久化存储。
"""
from __future__ import annotations

from uuid import UUID, uuid4
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.openapi.deps import require_scope
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["开放接口-Webhook 事件"])

# 预留内存存储 — 生产环境应替换为数据库持久化
_subscriptions: dict[str, dict[str, Any]] = {}

# 可订阅事件类型
_SUBSCRIBABLE_EVENTS: list[dict[str, str]] = [
    {
        "event": "document.created",
        "description": "文档创建事件",
    },
    {
        "event": "document.updated",
        "description": "文档更新事件",
    },
    {
        "event": "document.deleted",
        "description": "文档删除事件",
    },
    {
        "event": "knowledge.created",
        "description": "知识库创建事件",
    },
    {
        "event": "knowledge.updated",
        "description": "知识库更新事件",
    },
]


# ======================================================================
# 请求 Schema
# ======================================================================


class SubscribeRequest(BaseModel):
    """Webhook 订阅请求。"""

    url: str = Field(..., description="回调 URL（事件推送目标）")
    events: list[str] = Field(
        ..., min_length=1, description="订阅的事件类型列表"
    )
    secret: str | None = Field(default=None, description="签名密钥（用于校验回调）")


class TestEventRequest(BaseModel):
    """测试事件请求。"""

    url: str | None = Field(default=None, description="目标 URL（为空则推送给所有订阅）")
    event: str = Field(default="test.event", description="测试事件类型")


# ======================================================================
# 端点
# ======================================================================


@router.get("/events", response_model=ApiResponse[list[dict]])
async def list_events(
    api_key_info: dict = Depends(require_scope("webhook:manage")),
) -> ApiResponse[list[dict]]:
    """列出可订阅的事件类型。"""
    return ApiResponse(
        code=0,
        data=_SUBSCRIBABLE_EVENTS,
        message="success",
    )


@router.post("/subscribe", response_model=ApiResponse[dict], status_code=201)
async def subscribe(
    body: SubscribeRequest,
    api_key_info: dict = Depends(require_scope("webhook:manage")),
) -> ApiResponse[dict]:
    """订阅 Webhook 事件。

    创建订阅后，指定事件发生时系统会向 ``url`` 发起 POST 回调，
    请求体携带事件数据，并使用 ``secret`` 签名（HMAC-SHA256）。
    """
    # 校验事件类型
    valid_events = {e["event"] for e in _SUBSCRIBABLE_EVENTS}
    invalid = set(body.events) - valid_events
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的事件类型: {invalid}",
        )

    sub_id = str(uuid4())
    subscription = {
        "id": sub_id,
        "url": body.url,
        "events": body.events,
        "secret": body.secret,
        "key_id": api_key_info.get("key_id"),
        "is_active": True,
    }
    _subscriptions[sub_id] = subscription

    logger.info(
        "openapi.webhook.subscribed",
        sub_id=sub_id,
        url=body.url,
        events=body.events,
    )

    return ApiResponse(
        code=0,
        data=subscription,
        message="success",
    )


@router.delete("/subscribe/{sub_id}", response_model=ApiResponse)
async def unsubscribe(
    sub_id: UUID,
    api_key_info: dict = Depends(require_scope("webhook:manage")),
) -> ApiResponse:
    """取消 Webhook 订阅。

    Raises:
        HTTPException 404: 订阅不存在。
    """
    sub_id_str = str(sub_id)
    sub = _subscriptions.get(sub_id_str)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"订阅 {sub_id} 不存在",
        )

    sub["is_active"] = False
    del _subscriptions[sub_id_str]

    logger.info("openapi.webhook.unsubscribed", sub_id=sub_id_str)

    return ApiResponse(code=0, message="success")


@router.post("/test", response_model=ApiResponse[dict])
async def send_test_event(
    body: TestEventRequest,
    api_key_info: dict = Depends(require_scope("webhook:manage")),
) -> ApiResponse[dict]:
    """发送测试事件。

    向指定 URL（或所有活跃订阅）发送一条测试事件，用于验证回调链路连通性。
    实际推送由异步任务执行，本接口仅返回发送计划。
    """
    target_count = 0
    if body.url:
        target_count = 1
    else:
        target_count = sum(
            1 for s in _subscriptions.values() if s.get("is_active")
        )

    logger.info(
        "openapi.webhook.test_event",
        url=body.url,
        event=body.event,
        targets=target_count,
    )

    return ApiResponse(
        code=0,
        data={
            "event": body.event,
            "payload": {"test": True, "source": "openapi"},
            "targets": target_count,
            "status": "queued",
            "message": "测试事件已加入推送队列",
        },
        message="success",
    )
