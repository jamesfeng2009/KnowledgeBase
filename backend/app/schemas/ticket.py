"""
IT 工单请求/响应 Schema — 单一职责：HTTP 层数据契约。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TicketStatus = Literal["open", "progress", "resolved"]
TicketPriority = Literal["low", "medium", "high", "urgent"]


class TicketCreate(BaseModel):
    """创建工单请求体。"""

    title: str = Field(..., min_length=1, max_length=500, description="工单标题")
    description: str = Field(..., min_length=1, max_length=20_000, description="问题描述")
    category: str = Field(
        default="other",
        max_length=30,
        description="分类: account/network/software/hardware/access/other",
    )
    priority: TicketPriority = Field(default="medium", description="优先级")


class TicketUpdate(BaseModel):
    """更新工单请求体 — 仅更新传入字段。

    状态机由 Service 校验：resolved 为终态，不可回退。
    """

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, min_length=1, max_length=20_000)
    category: str | None = Field(default=None, max_length=30)
    priority: TicketPriority | None = None
    status: TicketStatus | None = None
    ai_answer: str | None = Field(default=None, max_length=20_000)
    ai_solution: str | None = Field(default=None, max_length=20_000)
    ai_solved: bool | None = None


class TicketResponse(BaseModel):
    """工单响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    category: str
    priority: str
    status: str
    user_name: str | None = None
    ai_answer: str | None = None
    ai_solution: str | None = None
    ai_solved: bool = False
    created_at: datetime
    updated_at: datetime | None = None
