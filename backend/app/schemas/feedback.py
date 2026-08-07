"""
用户反馈 Schema — 单一职责：反馈的创建、更新与响应数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，不包含反馈流转、通知等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FeedbackType(str, Enum):
    """反馈类型。"""

    bug = "bug"
    suggestion = "suggestion"
    praise = "praise"
    complaint = "complaint"


class FeedbackStatus(str, Enum):
    """反馈状态。"""

    open = "open"
    processing = "processing"
    resolved = "resolved"
    closed = "closed"


class FeedbackPriority(str, Enum):
    """反馈优先级。"""

    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class FeedbackCreate(BaseModel):
    """反馈创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    type: FeedbackType = Field(..., description="类型: bug/suggestion/praise/complaint")
    content: str = Field(..., min_length=1, description="反馈内容")
    priority: FeedbackPriority = Field(
        default=FeedbackPriority.normal, description="优先级"
    )
    related_message_id: uuid.UUID | None = Field(
        default=None, description="关联消息 ID"
    )
    doc_id: uuid.UUID | None = Field(
        default=None, description="关联文档 ID（可选，缺省时服务端从关联消息引用来源解析）"
    )


class FeedbackUpdate(BaseModel):
    """反馈更新请求 — 所有字段可选，用于状态流转与处理回复。"""

    model_config = ConfigDict(from_attributes=True)

    status: FeedbackStatus | None = Field(default=None, description="状态")
    priority: FeedbackPriority | None = Field(default=None, description="优先级")
    response: str | None = Field(default=None, description="处理回复")


class FeedbackResponse(BaseModel):
    """反馈响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="反馈 ID")
    user_id: uuid.UUID = Field(..., description="提交用户 ID")
    type: FeedbackType = Field(..., description="类型")
    content: str = Field(..., description="反馈内容")
    status: FeedbackStatus = Field(..., description="状态")
    priority: FeedbackPriority = Field(..., description="优先级")
    related_message_id: uuid.UUID | None = Field(
        default=None, description="关联消息 ID"
    )
    doc_id: uuid.UUID | None = Field(default=None, description="关联文档 ID")
    response: str | None = Field(default=None, description="处理回复")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")
