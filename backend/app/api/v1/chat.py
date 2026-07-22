"""
对话路由 — 单一职责：处理 AI 对话的 SSE 流式响应与对话历史查询。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（会话管理、RAG 引擎调用、消息持久化）委托给 ChatService。

SSE 说明：
ChatService.chat 是异步生成器，产出 SSEEvent | str 对象，
由 ``sse_response()`` 统一包装为 SSE 协议文本流（含 event/data 字段）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Request
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
from app.utils.logger import get_logger
from app.utils.sse import SSEEvent, SSEEventType, sse_response

log = get_logger(__name__)

router = APIRouter(tags=["AI 对话"])


async def _error_stream(message: str) -> AsyncIterator[SSEEvent]:
    """生成仅含 error 事件的 SSE 流。

    用于流式开始前的权限拒绝等场景 — 以 SSE error 事件返回友好错误，
    而不是裸 403 / 断流（前端 EventSource 可正常解析展示）。
    """
    yield SSEEvent(
        data={"type": "error", "message": message},
        event=SSEEventType.ERROR,
    )


@router.post("/chat/stream")
async def chat(
    request: Request,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
):
    """AI 对话 — SSE 流式返回。

    ChatService.chat 产出 SSEEvent | str 对象流，由 ``sse_response()`` 包装为
    ``text/event-stream`` 响应。事件类型包括：

    - ``event=meta``: 对话元数据（conversation_id, agent_type）；
    - ``event=thinking``: Agent 思考进度（P0-2）；
    - ``event=retrieve_start/retrieve_end``: 检索进度（P0-2）；
    - ``event=tool_call_start/tool_call_end``: 工具调用进度（P0-3）；
    - ``data``（默认）: 逐 token 的文本片段；
    - ``event=sources``: 引用来源（P0-2）；
    - ``event=quality``: 质量评分（P0-2）；
    - ``event=done``: 流结束标记（含 token_count / iterations）。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ChatService(db, user, tenant_id=tenant_id)

    # 准备阶段：完成全部必要 DB 读写（会话获取/创建、权限校验、
    # 用户消息持久化、记忆加载、模型解析）
    try:
        prepared = await service.prepare_chat(
            query=body.query,
            conversation_id=body.conversation_id,
            agent_type=body.agent_type.value,
        )
    except PermissionError as exc:
        # 权限异常 → SSE error 事件（前端可收到友好错误，而非断流）
        log.info(
            "chat.stream_permission_denied",
            error=str(exc),
            user_id=str(user.id),
        )
        return sse_response(_error_stream(str(exc)))

    # 流式开始前：提交准备阶段写入并释放 DB 连接回池 —
    # SSE 长连接期间不持有连接池连接（防高并发时池耗尽）；
    # 流式结束后的持久化由 service 内短事务完成。
    await db.commit()
    await db.close()

    generator = service.stream_chat(prepared)
    return sse_response(generator)


@router.get("/conversations", response_model=ApiResponse[list[ConversationResponse]])
async def list_conversations(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[ConversationResponse]]:
    """查询当前用户的所有对话列表。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ChatService(db, user, tenant_id=tenant_id)
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
    request: Request,
    conv_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[MessageResponse]]:
    """查询指定对话下的全部消息（按时间正序）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ChatService(db, user, tenant_id=tenant_id)
    messages = await service.get_conversation_messages(conv_id)
    return ApiResponse(
        code=0,
        data=[MessageResponse.model_validate(msg) for msg in messages],
        message="success",
    )
