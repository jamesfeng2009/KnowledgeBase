"""
问答社区 Schema — 单一职责：问答帖与回答的请求/响应数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，不包含采纳、投票等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class QaQuestionStatus(str, Enum):
    """问答帖状态。"""

    open = "open"
    answered = "answered"
    closed = "closed"


class QaQuestionCreate(BaseModel):
    """问答帖创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    kb_id: uuid.UUID | None = Field(default=None, description="关联知识库 ID")
    title: str = Field(..., min_length=1, max_length=500, description="问题标题")
    content: str = Field(..., min_length=1, description="问题详情")
    tags: str | None = Field(
        default=None, max_length=500, description="标签（逗号分隔）"
    )


class QaQuestionResponse(BaseModel):
    """问答帖响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="问题 ID")
    user_id: uuid.UUID = Field(..., description="提问者 ID")
    kb_id: uuid.UUID | None = Field(default=None, description="关联知识库 ID")
    title: str = Field(..., description="问题标题")
    content: str = Field(..., description="问题详情")
    status: QaQuestionStatus = Field(..., description="状态")
    view_count: int = Field(default=0, ge=0, description="浏览数")
    answer_count: int = Field(default=0, ge=0, description="回答数")
    tags: str | None = Field(default=None, description="标签（逗号分隔）")
    created_at: datetime = Field(..., description="创建时间")


class QaAnswerCreate(BaseModel):
    """回答创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    question_id: uuid.UUID = Field(..., description="所属问题 ID")
    content: str = Field(..., min_length=1, description="回答内容")
    is_ai_generated: bool = Field(default=False, description="是否 AI 生成")


class QaAnswerResponse(BaseModel):
    """回答响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="回答 ID")
    question_id: uuid.UUID = Field(..., description="所属问题 ID")
    user_id: uuid.UUID = Field(..., description="回答者 ID")
    content: str = Field(..., description="回答内容")
    is_accepted: bool = Field(default=False, description="是否被采纳")
    is_ai_generated: bool = Field(default=False, description="是否 AI 生成")
    vote_count: int = Field(default=0, ge=0, description="投票数")
    created_at: datetime = Field(..., description="创建时间")
