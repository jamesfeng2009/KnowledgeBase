"""
对话路由 — 单一职责：处理 AI 对话的 SSE 流式响应与对话历史查询。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（会话管理、LLM 调用、消息持久化）委托给 ChatService。

SSE 说明：
ChatService.chat 是异步生成器，已产出 SSE 协议文本块（format_sse_event），
因此直接使用 StreamingResponse 透传，不再二次封装。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.conversation import (
    ChatRequest,
    ConversationResponse,
    MessageResponse,
)
from app.services.chat_service import ChatService

router = APIRouter(tags=["AI 对话"])


@router.post("/chat")
async def chat(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """AI 对话 — SSE 流式返回。

    ChatService.chat 产出 SSE 格式的 token 流（含元数据与结束事件），
    本端点直接以 ``text/event-stream`` 透传，不使用 ApiResponse 包装。

    流事件类型：
    - ``event=meta``: 对话元数据（conversation_id, agent_type）；
    - ``data``（默认）: 逐 token 的文本片段；
    - ``event=done``: 流结束标记。
    """
    service = ChatService(db, user)
    generator = service.chat(
        query=body.query,
        conversation_id=body.conversation_id,
        agent_type=body.agent_type.value,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations", response_model=ApiResponse[list[ConversationResponse]])
async def list_conversations(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[ConversationResponse]]:
    """查询当前用户的所有对话列表。"""
    service = ChatService(db, user)
    conversations = await service.get_conversations()
    return ApiResponse(
        code=0,
        data=[ConversationResponse.model_validate(conv) for conv in conversations],
        message="success",
    )


@router.get(
    "/conversations/{conv_id}/messages",
    response_model=ApiResponse[list[MessageResponse]],
)
async def get_conversation_messages(
    conv_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[MessageResponse]]:
    """查询指定对话下的全部消息（按时间正序）。"""
    service = ChatService(db, user)
    messages = await service.get_conversation_messages(conv_id)
    return ApiResponse(
        code=0,
        data=[MessageResponse.model_validate(msg) for msg in messages],
        message="success",
    )
