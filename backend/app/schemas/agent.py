"""
Agent Schema — 单一职责：Agent 配置与调用的请求/响应数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，
不包含 Agent 执行、工具调用等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.conversation import AgentType


class AgentInfo(BaseModel):
    """Agent 信息 — 列表与详情的统一响应结构。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Agent ID")
    name: str = Field(..., description="Agent 名称")
    type: AgentType = Field(..., description="Agent 类型: qa/workflow/action")
    description: str | None = Field(default=None, description="Agent 描述")
    enabled: bool = Field(default=True, description="是否启用")
    config: dict[str, Any] | None = Field(
        default=None, description="Agent 专属配置（system_prompt/tools/model 等）"
    )
    created_at: datetime = Field(..., description="创建时间")


class AgentCreate(BaseModel):
    """Agent 创建请求 — 仅 admin 可创建。"""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., min_length=1, max_length=255, description="Agent 名称")
    type: AgentType = Field(default=AgentType.qa, description="Agent 类型")
    description: str | None = Field(default=None, description="Agent 描述")
    config: dict[str, Any] | None = Field(
        default=None, description="Agent 专属配置"
    )


class AgentUpdate(BaseModel):
    """Agent 更新请求 — 所有字段可选。"""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class AgentInvokeRequest(BaseModel):
    """Agent 调用请求 — 返回 SSE 流。"""

    model_config = ConfigDict(from_attributes=True)

    query: str = Field(..., min_length=1, description="用户输入")
    session_id: uuid.UUID | None = Field(
        default=None, description="会话 ID（为空则新建会话）"
    )
    context: dict[str, Any] | None = Field(
        default=None, description="附加上下文（如用户偏好、历史摘要等）"
    )


class AgentListResponse(BaseModel):
    """Agent 列表响应。"""

    model_config = ConfigDict(from_attributes=True)

    agents: list[AgentInfo] = Field(
        default_factory=list, description="Agent 列表"
    )
