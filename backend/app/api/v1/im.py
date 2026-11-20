"""
IM 集成 API — 单一职责：企业微信回调接收与机器人状态查询。

路由：
    POST /im/wecom/callback     — 企微回调验签 + 事件接收（回显 echostr）
    GET  /im/wecom/status       — 机器人/连接器配置状态
    POST /im/wecom/bot/markdown — 手动推送 markdown 到群机器人
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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

    content: str = Field(
        ..., min_length=1, max_length=4096, description="Markdown 内容"
    )


class WeComCallbackQuery(BaseModel):
    """企微回调 URL 参数。"""

    msg_signature: str
    timestamp: str
    nonce: str
    echostr: str | None = None


# 企微 POST 回调 body 形如
#   <xml>...<Encrypt><![CDATA[BASE64...]]></Encrypt>...</xml>
# 签名第 4 个参数是 Encrypt 内容（GET 时是 echostr）。
_ENCRYPT_RE = re.compile(
    r"<Encrypt>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</Encrypt>", re.DOTALL
)


def _verify_wecom_callback(
    *, msg_signature: str, timestamp: str, nonce: str, signed_field: str
) -> None:
    """验签 + 配置缺失守卫；失败抛 HTTPException。

    ``WECOM_BOT_TOKEN`` 默认为空字符串。空 token 时 sha1(sorted(...)) 仍然
    算得出来，攻击者只要知道算法就能自己构造签名 —— 所以配置缺失必须
    显式拒绝，不能当成「开发环境放行」。
    """
    token = (get_settings().WECOM_BOT_TOKEN or "").strip()
    if not token:
        raise HTTPException(503, detail="企微回调验签 token 未配置")
    if not WeComBotService.verify_signature(
        token, timestamp, nonce, signed_field, msg_signature
    ):
        raise HTTPException(403, detail="签名校验失败")


@router.get("/wecom/callback")
async def wecom_callback_verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(default=""),
) -> ApiResponse[str]:
    """企微回调 URL 验证（GET 回显 echostr）。"""
    _verify_wecom_callback(
        msg_signature=msg_signature,
        timestamp=timestamp,
        nonce=nonce,
        signed_field=echostr,
    )
    return ApiResponse(code=0, data=echostr, message="success")


@router.post("/wecom/callback")
async def wecom_callback_events(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> ApiResponse[dict]:
    """企微事件回调接收（POST）— 先验签，事件处理仍由事件总线接入。

    此前这个端点**没有任何验签**，任何匿名 POST 都能拿到 200。它现在不查库、
    不派发任务，危害有限，但只要将来接上事件总线，就会变成一条无门槛的
    写入口。公开端点的签名校验不能等「以后有逻辑了再补」。
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    match = _ENCRYPT_RE.search(raw)
    signed_field = match.group(1).strip() if match else ""
    _verify_wecom_callback(
        msg_signature=msg_signature,
        timestamp=timestamp,
        nonce=nonce,
        signed_field=signed_field,
    )
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
        raise HTTPException(
            status_code=502, detail="推送失败（检查 WECOM_BOT_WEBHOOK）"
        )
    return ApiResponse(code=0, data=True, message="success")
