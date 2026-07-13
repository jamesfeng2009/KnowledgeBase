"""
Agent 管理路由 — 单一职责：处理 Agent 配置与调用的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
Agent 执行逻辑（推理、工具调用、SSE 流）委托给 ChatService。

Agent 调用返回 SSE 流式响应，前端通过 EventSource 接收实时增量。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.agent import AgentConfig
from app.models.user import User
from app.schemas.agent import (
    AgentCreate,
    AgentInfo,
    AgentInvokeRequest,
    AgentListResponse,
    AgentUpdate,
)
from app.schemas.common import ApiResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Agent 管理"])


def _require_admin(user: User) -> None:
    """校验当前用户是否为管理员。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可执行此操作",
        )


@router.get("/agents", response_model=ApiResponse[AgentListResponse])
async def list_agents(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[AgentListResponse]:
    """获取 Agent 列表（仅返回已启用的 Agent）。"""
    stmt = (
        select(AgentConfig)
        .where(AgentConfig.is_enabled.is_(True))
        .order_by(AgentConfig.created_at.desc())
    )
    result = await db.execute(stmt)
    agents = list(result.scalars().all())

    return ApiResponse(
        code=0,
        data=AgentListResponse(
            agents=[
                AgentInfo(
                    id=a.id,
                    name=a.name,
                    type=a.type,
                    description=a.description,
                    enabled=a.is_enabled,
                    config=a.config,
                    created_at=a.created_at,
                )
                for a in agents
            ]
        ),
        message="success",
    )


@router.get("/agents/{agent_id}", response_model=ApiResponse[AgentInfo])
async def get_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[AgentInfo]:
    """获取 Agent 详情。"""
    stmt = select(AgentConfig).where(AgentConfig.id == agent_id)
    result = await db.execute(stmt)
    agent = result.scalars().first()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} 不存在",
        )

    return ApiResponse(
        code=0,
        data=AgentInfo(
            id=agent.id,
            name=agent.name,
            type=agent.type,
            description=agent.description,
            enabled=agent.is_enabled,
            config=agent.config,
            created_at=agent.created_at,
        ),
        message="success",
    )


@router.post("/agents/{agent_id}/invoke")
async def invoke_agent(
    agent_id: uuid.UUID,
    body: AgentInvokeRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """调用 Agent，返回 SSE 流式响应。

    每条 SSE 事件格式::

        data: {"content": "增量文本", "done": false}

    流结束时发送::

        data: {"content": "", "done": true}
    """
    # 查询 Agent 配置
    stmt = select(AgentConfig).where(
        AgentConfig.id == agent_id,
        AgentConfig.is_enabled.is_(True),
    )
    result = await db.execute(stmt)
    agent = result.scalars().first()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} 不存在或已禁用",
        )

    # 尝试调用 ChatService 进行 SSE 流式响应
    try:
        from app.services.chat_service import ChatService

        chat_service = ChatService(db, user)

        async def event_stream():
            """SSE 事件流生成器。"""
            try:
                async for chunk in chat_service.stream_agent_response(
                    query=body.query,
                    agent_config=agent,
                    session_id=body.session_id,
                    context=body.context,
                ):
                    import json

                    yield f"data: {json.dumps({'content': chunk, 'done': False})}\n\n"
                yield f"data: {json.dumps({'content': '', 'done': True})}\n\n"
            except Exception:
                logger.exception("Agent SSE 流生成失败")
                import json

                yield f"data: {json.dumps({'content': '', 'done': True, 'error': '内部错误'})}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except ImportError:
        logger.warning("ChatService 未就绪，返回占位响应")
        import json

        async def placeholder_stream():
            yield f"data: {json.dumps({'content': 'Agent 服务尚未就绪', 'done': True})}\n\n"

        return StreamingResponse(
            placeholder_stream(),
            media_type="text/event-stream",
        )


@router.post("/agents", response_model=ApiResponse[AgentInfo], status_code=201)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[AgentInfo]:
    """创建自定义 Agent（仅 admin 权限）。"""
    _require_admin(user)

    agent = AgentConfig(
        name=body.name,
        type=body.type.value,
        description=body.description,
        config=body.config,
        is_enabled=True,
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)

    return ApiResponse(
        code=0,
        data=AgentInfo(
            id=agent.id,
            name=agent.name,
            type=agent.type,
            description=agent.description,
            enabled=agent.is_enabled,
            config=agent.config,
            created_at=agent.created_at,
        ),
        message="success",
    )


@router.put("/agents/{agent_id}", response_model=ApiResponse[AgentInfo])
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[AgentInfo]:
    """更新 Agent 配置（仅 admin 权限）。"""
    _require_admin(user)

    stmt = select(AgentConfig).where(AgentConfig.id == agent_id)
    result = await db.execute(stmt)
    agent = result.scalars().first()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} 不存在",
        )

    update_fields = body.model_dump(exclude_unset=True)
    # 映射 enabled -> is_enabled
    if "enabled" in update_fields:
        agent.is_enabled = update_fields.pop("enabled")
    for key, value in update_fields.items():
        setattr(agent, key, value)
    await db.flush()
    await db.refresh(agent)

    return ApiResponse(
        code=0,
        data=AgentInfo(
            id=agent.id,
            name=agent.name,
            type=agent.type,
            description=agent.description,
            enabled=agent.is_enabled,
            config=agent.config,
            created_at=agent.created_at,
        ),
        message="success",
    )
