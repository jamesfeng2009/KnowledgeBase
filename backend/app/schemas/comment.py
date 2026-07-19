"""
文档评论 Schema — 单一职责：文档评论的请求/响应数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，不包含评论解析、通知等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CommentCreate(BaseModel):
    """文档评论创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    doc_id: uuid.UUID = Field(..., description="文档 ID")
    content: str = Field(..., min_length=1, description="评论内容")
    parent_id: uuid.UUID | None = Field(
        default=None, description="父评论 ID，为空表示顶级评论"
    )


class CommentResponse(BaseModel):
    """文档评论响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="评论 ID")
    doc_id: uuid.UUID = Field(..., description="文档 ID")
    user_id: uuid.UUID = Field(..., description="评论者 ID")
    content: str = Field(..., description="评论内容")
    parent_id: uuid.UUID | None = Field(default=None, description="父评论 ID")
    resolved: bool = Field(default=False, description="是否已解决")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")
