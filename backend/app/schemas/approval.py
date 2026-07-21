"""
工具审批 Schema — 单一职责：审批请求/响应的数据验证与序列化。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolApprovalResponse(BaseModel):
    """审批记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="审批 ID")
    session_id: str = Field(..., description="会话 ID")
    tool_name: str = Field(..., description="工具名称")
    tool_use_id: str = Field(..., description="工具调用 ID")
    tool_arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    reason: str = Field("", description="拦截原因")
    irreversible: bool = Field(False, description="是否不可逆")
    status: str = Field("pending", description="审批状态")
    created_at: datetime = Field(..., description="创建时间")
    resolved_at: datetime | None = Field(None, description="处理时间")
    expire_at: datetime | None = Field(None, description="过期时间")


class ApprovalActionRequest(BaseModel):
    """审批操作请求（approve / reject 通用）。"""

    comment: str | None = Field(default=None, max_length=500, description="审批备注")


class ApprovalListResponse(BaseModel):
    """审批列表响应。"""

    pending: list[ToolApprovalResponse] = Field(
        default_factory=list, description="待审批列表"
    )
    total: int = Field(0, description="待审批总数")
