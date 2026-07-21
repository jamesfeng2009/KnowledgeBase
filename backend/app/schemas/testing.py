"""
智能测试平台 Schema — 单一职责：测试平台的入参校验与出参序列化。

遵循分层架构：仅负责数据验证与序列化，不包含业务逻辑。
所有枚举值与 models/testing.py 中的 comment 保持一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ======================================================================
# 枚举定义
# ======================================================================


class RequirementCategory(str, Enum):
    """需求分类。"""

    functional = "functional"
    non_functional = "non_functional"
    ui = "ui"
    api = "api"
    performance = "performance"


class RequirementStatus(str, Enum):
    """需求状态。"""

    pending = "pending"
    analyzed = "analyzed"
    generating_cases = "generating_cases"
    cases_ready = "cases_ready"


class TestCaseType(str, Enum):
    """测试类型。"""

    functional = "functional"
    api = "api"
    ui = "ui"
    performance = "performance"
    security = "security"
    compatibility = "compatibility"


class TestCaseStatus(str, Enum):
    """用例状态。"""

    draft = "draft"
    pending_review = "pending_review"
    approved = "approved"
    active = "active"
    deprecated = "deprecated"


class TestReviewStatus(str, Enum):
    """评审状态。"""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class TestPlanStatus(str, Enum):
    """测试计划状态。"""

    draft = "draft"
    active = "active"
    completed = "completed"
    archived = "archived"


class ExecutionStatus(str, Enum):
    """执行状态。"""

    pending = "pending"
    running = "running"
    passed = "passed"
    failed = "failed"
    blocked = "blocked"
    skipped = "skipped"


class ExecutionStrategy(str, Enum):
    """执行策略。"""

    sequential = "sequential"
    parallel = "parallel"
    priority_based = "priority_based"


class Priority(str, Enum):
    """优先级。"""

    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


# ======================================================================
# 测试项目 Schema
# ======================================================================


class TestProjectCreate(BaseModel):
    """创建测试项目。"""

    name: str = Field(..., min_length=1, max_length=255, description="项目名称")
    description: str | None = Field(default=None, description="项目描述")
    prd_doc_ids: list[str] | None = Field(default=None, description="PRD 文档 ID 列表")
    tech_doc_ids: list[str] | None = Field(default=None, description="技术方案文档 ID 列表")
    api_doc_ids: list[str] | None = Field(default=None, description="接口文档 ID 列表")


class TestProjectUpdate(BaseModel):
    """更新测试项目。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    prd_doc_ids: list[str] | None = None
    tech_doc_ids: list[str] | None = None
    api_doc_ids: list[str] | None = None
    status: str | None = None


class TestProjectResponse(BaseModel):
    """测试项目响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    owner_id: uuid.UUID
    prd_doc_ids: list | None = None
    tech_doc_ids: list | None = None
    api_doc_ids: list | None = None
    status: str
    created_at: datetime
    updated_at: datetime


# ======================================================================
# 需求点 Schema
# ======================================================================


class RequirementExtractRequest(BaseModel):
    """需求提取请求 — 从 PRD/UI 稿自动提取需求点。"""

    project_id: str = Field(..., description="项目 ID")
    doc_id: str = Field(..., description="来源文档 ID（知识库 Document）")
    # 可选：指定提取的需求分类，不指定则自动分类
    target_categories: list[RequirementCategory] | None = Field(
        default=None, description="目标需求分类（不指定则自动分类）"
    )


class TestRequirementResponse(BaseModel):
    """需求点响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    source_doc_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    category: str
    priority: str
    acceptance_criteria: list | None = None
    source: str
    status: str
    created_at: datetime


class TestRequirementUpdate(BaseModel):
    """更新需求点。"""

    title: str | None = None
    description: str | None = None
    category: str | None = None
    priority: str | None = None
    acceptance_criteria: list | None = None
    status: str | None = None


# ======================================================================
# 测试用例 Schema
# ======================================================================


class TestCaseGenerateRequest(BaseModel):
    """用例生成请求 — 基于需求点 + 上下文文档生成用例。"""

    requirement_id: str = Field(..., description="需求 ID")
    # 可选：额外上下文文档 ID（技术方案、接口文档等）
    context_doc_ids: list[str] | None = Field(
        default=None, description="额外上下文文档 ID 列表"
    )
    test_type: TestCaseType | None = Field(
        default=None, description="指定测试类型（不指定则自动判断）"
    )
    # 生成数量上限
    max_cases: int = Field(default=5, ge=1, le=20, description="最大生成用例数")


class TestStep(BaseModel):
    """测试步骤。"""

    step_no: int = Field(..., description="步骤序号")
    action: str = Field(..., description="操作描述")
    expected: str = Field(..., description="预期结果")


class TestCaseCreate(BaseModel):
    """手动创建测试用例。"""

    project_id: str = Field(..., description="项目 ID")
    requirement_id: str | None = Field(default=None, description="关联需求 ID")
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    preconditions: str | None = None
    test_steps: list[TestStep] | None = None
    expected_result: str | None = None
    test_type: TestCaseType = TestCaseType.functional
    priority: Priority = Priority.normal
    tags: list[str] | None = None


class TestCaseUpdate(BaseModel):
    """更新测试用例。"""

    title: str | None = None
    description: str | None = None
    preconditions: str | None = None
    test_steps: list[TestStep] | None = None
    expected_result: str | None = None
    test_type: str | None = None
    priority: str | None = None
    status: str | None = None
    tags: list[str] | None = None


class TestCaseResponse(BaseModel):
    """测试用例响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    requirement_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    preconditions: str | None = None
    test_steps: list | None = None
    expected_result: str | None = None
    test_type: str
    priority: str
    status: str
    tags: list | None = None
    created_by: str
    case_no: str | None = None
    context_doc_ids: list | None = None
    created_at: datetime
    updated_at: datetime


# ======================================================================
# 用例评审 Schema
# ======================================================================


class TestReviewSubmit(BaseModel):
    """提交用例评审。"""

    case_id: str = Field(..., description="用例 ID")
    comment: str | None = Field(default=None, description="评审备注")


class TestReviewAction(BaseModel):
    """评审动作。"""

    comment: str | None = Field(default=None, description="评审意见")
    suggestions: list[dict] | None = Field(default=None, description="评审建议列表")


class TestReviewResponse(BaseModel):
    """评审响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID
    submitter_id: uuid.UUID
    reviewer_id: uuid.UUID | None = None
    status: str
    comment: str | None = None
    suggestions: list | None = None
    review_summary: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


# ======================================================================
# 测试计划 Schema
# ======================================================================


class TestPlanCreate(BaseModel):
    """创建测试计划。"""

    project_id: str = Field(..., description="项目 ID")
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    case_ids: list[str] | None = Field(default=None, description="用例 ID 列表")
    execution_strategy: ExecutionStrategy = ExecutionStrategy.priority_based


class TestPlanOrchestrateRequest(BaseModel):
    """AI 编排请求。"""

    # 可选：指定执行节点数量
    node_count: int = Field(default=3, ge=1, le=10, description="执行节点数量")
    # 可选：是否考虑用例依赖关系
    consider_dependencies: bool = Field(default=True, description="是否考虑用例依赖关系")


class TestPlanResponse(BaseModel):
    """测试计划响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None = None
    case_ids: list | None = None
    execution_strategy: str
    ai_orchestration: dict | None = None
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ======================================================================
# 执行记录 Schema
# ======================================================================


class TestExecutionCreate(BaseModel):
    """记录执行结果。"""

    plan_id: str | None = Field(default=None, description="测试计划 ID")
    case_id: str = Field(..., description="用例 ID")
    executor: str = Field(default="human", description="执行者: human/ai")
    status: ExecutionStatus = ExecutionStatus.pending
    result: str | None = None
    execution_log: dict | None = None
    failure_reason: str | None = None
    duration_seconds: int = Field(default=0, ge=0)


class TestExecutionResponse(BaseModel):
    """执行记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plan_id: uuid.UUID | None = None
    case_id: uuid.UUID
    executor_id: uuid.UUID | None = None
    executor: str
    status: str
    result: str | None = None
    execution_log: dict | None = None
    failure_reason: str | None = None
    duration_seconds: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


# ======================================================================
# 统计 Schema
# ======================================================================


class TestingStatsResponse(BaseModel):
    """测试平台统计响应。"""

    total_projects: int = 0
    total_requirements: int = 0
    total_cases: int = 0
    cases_by_status: dict[str, int] = Field(default_factory=dict)
    cases_by_type: dict[str, int] = Field(default_factory=dict)
    total_plans: int = 0
    total_executions: int = 0
    pass_rate: float = 0.0
    execution_stats: dict[str, int] = Field(default_factory=dict)
