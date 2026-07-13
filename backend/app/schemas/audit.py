"""
审核流程 Schema — 单一职责：审核流程响应与审核动作的数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，不包含审核流转、通知等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ResourceType(str, Enum):
    """受审核资源类型。"""

    document = "document"
    kb = "kb"
    question = "question"


class AuditStatus(str, Enum):
    """审核状态。"""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class AuditPriority(str, Enum):
    """审核优先级。"""

    low = "low"
    normal = "normal"
    high = "high"


class AuditActionType(str, Enum):
    """审核动作 — approve 通过 / reject 驳回。"""

    approve = "approve"
    reject = "reject"


class AuditAction(BaseModel):
    """审核动作请求 — approve/reject + 审核意见。"""

    model_config = ConfigDict(from_attributes=True)

    action: AuditActionType = Field(
        ..., description="审核动作: approve 通过 / reject 驳回"
    )
    comment: str | None = Field(default=None, description="审核意见")


class AuditFlowResponse(BaseModel):
    """审核流程响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="审核流程 ID")
    resource_type: ResourceType = Field(..., description="资源类型")
    resource_id: uuid.UUID = Field(..., description="资源 ID")
    submitter_id: uuid.UUID = Field(..., description="提交者 ID")
    reviewer_id: uuid.UUID | None = Field(default=None, description="审核者 ID")
    status: AuditStatus = Field(..., description="状态")
    comment: str | None = Field(default=None, description="审核意见")
    priority: AuditPriority = Field(..., description="优先级")
    created_at: datetime = Field(..., description="创建时间")
