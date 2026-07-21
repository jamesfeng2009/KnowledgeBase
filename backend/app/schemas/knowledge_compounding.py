"""
知识回流层 Schema — 单一职责：知识回流入参校验与出参序列化。

遵循分层架构：仅负责数据验证与序列化，不包含业务逻辑。
所有枚举值与 models/knowledge_compounding.py 中的 comment 保持一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ======================================================================
# 枚举定义
# ======================================================================


class AssetType(str, Enum):
    """知识资产类型。"""

    defect_experience = "defect_experience"
    regression_sop = "regression_sop"
    graph_association = "graph_association"
    verification_baseline = "verification_baseline"


class AssetStatus(str, Enum):
    """知识资产状态。"""

    draft = "draft"
    active = "active"
    deprecated = "deprecated"
    conflict = "conflict"


class TaskType(str, Enum):
    """回流任务类型。"""

    extraction = "extraction"
    conflict_detection = "conflict_detection"
    reuse_injection = "reuse_injection"


class TaskStatus(str, Enum):
    """回流任务状态。"""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class ConflictType(str, Enum):
    """冲突类型。"""

    contradiction = "contradiction"
    supersede = "supersede"
    overlap = "overlap"


class ConflictResolution(str, Enum):
    """冲突解决方案。"""

    new_wins = "new_wins"
    existing_wins = "existing_wins"
    merged = "merged"
    pending = "pending"


# ======================================================================
# 知识资产 Schema
# ======================================================================


class KnowledgeAssetResponse(BaseModel):
    """知识资产响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    asset_type: str
    source_type: str
    source_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    title: str
    content: str
    summary: str | None = None
    tags: list[str] | None = None
    doc_id: uuid.UUID | None = None
    graph_nodes: list | None = None
    graph_relationships: list | None = None
    graphiti_entity_id: uuid.UUID | None = None
    confidence_score: float | None = None
    status: str
    conflict_with: list[str] | None = None
    compounding_task_id: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ======================================================================
# 回流任务 Schema
# ======================================================================


class CompoundingTaskResponse(BaseModel):
    """回流任务响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    execution_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    task_type: str
    status: str
    trigger_source: str
    extracted_asset_ids: list[str] | None = None
    conflicts_detected: int = 0
    assets_injected: int = 0
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None


class ExtractionRequest(BaseModel):
    """触发知识提取请求。"""

    execution_id: str = Field(..., description="执行记录 ID（UUID 字符串）")
    trigger_source: str = Field(
        default="manual", description="触发来源: execution_completed/manual/scheduled"
    )


class ConflictDetectionRequest(BaseModel):
    """触发冲突检测请求。"""

    asset_id: str = Field(..., description="待检测的知识资产 ID（UUID 字符串）")


class ReuseInjectionRequest(BaseModel):
    """触发复用注入请求。"""

    requirement_id: str = Field(..., description="需求点 ID（UUID 字符串）")
    max_assets: int = Field(default=5, ge=1, le=20, description="最大注入资产数")


# ======================================================================
# 知识冲突 Schema
# ======================================================================


class KnowledgeConflictResponse(BaseModel):
    """知识冲突响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    new_asset_id: uuid.UUID
    existing_asset_id: uuid.UUID
    conflict_type: str
    description: str | None = None
    resolution: str
    resolved_by: uuid.UUID | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    created_at: datetime | None = None


class ConflictResolveRequest(BaseModel):
    """解决冲突请求。"""

    resolution: str = Field(
        ..., description="解决方案: new_wins/existing_wins/merged"
    )
    note: str | None = Field(default=None, description="解决备注")


# ======================================================================
# 复用注入结果 Schema
# ======================================================================


class ReuseInjectionResult(BaseModel):
    """复用注入结果。"""

    requirement_id: str
    injected_assets: list[dict] = Field(default_factory=list)
    injection_context: str | None = None
    asset_count: int = 0


# ======================================================================
# 回流统计 Schema
# ======================================================================


class CompoundingStatsResponse(BaseModel):
    """知识回流统计。"""

    total_assets: int = 0
    assets_by_type: dict[str, int] = Field(default_factory=dict)
    assets_by_status: dict[str, int] = Field(default_factory=dict)
    total_tasks: int = 0
    tasks_by_status: dict[str, int] = Field(default_factory=dict)
    total_conflicts: int = 0
    unresolved_conflicts: int = 0
    reuse_injection_count: int = 0
