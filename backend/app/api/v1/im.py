"""
IM 集成 API — 单一职责：企业微信回调接收与机器人状态查询。

路由：
    POST /im/wecom/callback     — 企微回调验签 + 事件接收（回显 echostr）
    GET  /im/wecom/status       — 机器人/连接器配置状态
    POST /im/wecom/bot/markdown — 手动推送 markdown 到群机器人
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.wecom_bot_service import WeComBotService

router = APIRouter(prefix="/im", tags=["IM 集成"])


class MarkdownPushBody(BaseModel):
    """群机器人 markdown 推送请求体。"""

    content: str = Field(..., min_length=1, max_length=4096, description="Markdown 内容")


class WeComCallbackQuery(BaseModel):
    """企微回调 URL 参数。"""

    msg_signature: str
    timestamp: str
    nonce: str
    echostr: str | None = None


@router.get("/wecom/callback")
async def wecom_callback_verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(default=""),
) -> ApiResponse[str]:
    """企微回调 URL 验证（GET 回显 echostr）。"""
    settings = get_settings()
    service = WeComBotService()
    if not service.verify_signature(
        settings.WECOM_BOT_TOKEN, timestamp, nonce, echostr, msg_signature
    ):
        raise HTTPException(status_code=403, detail="签名校验失败")
    return ApiResponse(code=0, data=echostr, message="success")


@router.post("/wecom/callback")
async def wecom_callback_events() -> ApiResponse[dict]:
    """企微事件回调接收（POST）— 当前仅确认接收，处理逻辑由事件总线接入。"""
    return ApiResponse(code=0, data={"received": True}, message="success")


@router.get("/wecom/status")
async def wecom_status(
    _user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """查询企微集成配置状态（是否启用/是否已配置 webhook）。"""
    settings = get_settings()
    return ApiResponse(
        code=0,
        data={
            "enabled": bool(settings.WECOM_CORP_ID and settings.WECOM_SECRET),
            "bot_webhook_configured": bool(settings.WECOM_BOT_WEBHOOK),
            "callback_configured": bool(settings.WECOM_BOT_TOKEN),
        },
        message="success",
    )


@router.post("/wecom/bot/markdown", response_model=ApiResponse[bool])
async def send_markdown(
    body: MarkdownPushBody,
    db: AsyncSession = Depends(get_db_session),
    _user: User = Depends(get_current_active_user),
) -> ApiResponse[bool]:
    """手动向企微群机器人推送 markdown 消息。"""
    del db
    service = WeComBotService()
    ok = await service.send_markdown(body.content)
    if not ok:
        raise HTTPException(status_code=502, detail="推送失败（检查 WECOM_BOT_WEBHOOK）")
    return ApiResponse(code=0, data=True, message="success")
