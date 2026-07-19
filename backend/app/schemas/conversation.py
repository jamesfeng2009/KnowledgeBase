"""
对话与消息 Schema — 单一职责：AI 对话会话与消息的请求/响应数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，不包含检索、生成等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentType(str, Enum):
    """Agent 类型。"""

    qa = "qa"
    workflow = "workflow"
    action = "action"


class MessageRole(str, Enum):
    """消息角色。"""

    user = "user"
    assistant = "assistant"
    system = "system"


class ConversationCreate(BaseModel):
    """对话创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    title: str = Field(default="新对话", max_length=255, description="对话标题")
    agent_type: AgentType = Field(default=AgentType.qa, description="Agent 类型")


class ConversationResponse(BaseModel):
    """对话响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="对话 ID")
    user_id: uuid.UUID = Field(..., description="用户 ID")
    title: str = Field(..., description="对话标题")
    agent_type: AgentType = Field(..., description="Agent 类型")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class MessageCreate(BaseModel):
    """消息创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    conversation_id: uuid.UUID = Field(..., description="所属对话 ID")
    role: MessageRole = Field(..., description="角色: user/assistant/system")
    content: str = Field(..., min_length=1, description="消息内容")
    sources: list[dict[str, Any]] | None = Field(
        default=None, description="引用来源列表"
    )
    token_count: int = Field(default=0, ge=0, description="Token 消耗")
    model_used: str | None = Field(default=None, max_length=100, description="使用的模型")


class MessageResponse(BaseModel):
    """消息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="消息 ID")
    conversation_id: uuid.UUID = Field(..., description="所属对话 ID")
    role: MessageRole = Field(..., description="角色: user/assistant/system")
    content: str = Field(..., description="消息内容")
    sources: list[dict[str, Any]] | None = Field(
        default=None, description="引用来源列表"
    )
    token_count: int = Field(default=0, ge=0, description="Token 消耗")
    model_used: str | None = Field(default=None, description="使用的模型")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class ChatRequest(BaseModel):
    """AI 问答请求 — 用户提问入口。"""

    model_config = ConfigDict(from_attributes=True)

    query: str = Field(..., min_length=1, description="用户提问内容")
    conversation_id: uuid.UUID | None = Field(
        default=None, description="对话 ID，为空则新建对话"
    )
    agent_type: AgentType = Field(default=AgentType.qa, description="Agent 类型")
