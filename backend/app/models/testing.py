"""
智能测试平台模型 — 单一职责：定义测试项目、需求点、测试用例、评审、计划、执行记录表。

核心流程：
    PRD/UI稿 → 需求提取(TestRequirement) → 用例生成(TestCase)
    → 用例评审(TestReview) → 用例管理(TestCase CRUD)
    → AI编排(TestPlan) → 执行记录(TestExecution)

复用现有能力：
    - Document 表：PRD/技术方案/接口文档存储在知识库 Document 中
    - AuditFlow 模式：用例评审复用审核工作流的 pending/approved/rejected 模式
    - LLM Provider：需求提取和用例生成通过 LLMProvider 抽象调用
    - 多租户：tenant_id 字段预留，与 KnowledgeBase/Document 一致
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


# ======================================================================
# 测试项目 — 顶层容器，关联 PRD/技术方案/接口文档
# ======================================================================


class TestProject(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """测试项目表 — 一个项目对应一个迭代/功能模块的测试工作。

    关联文档：
        - prd_doc_ids: PRD 文档 ID 列表（知识库 Document）
        - tech_doc_ids: 技术方案文档 ID 列表
        - api_doc_ids: 接口文档 ID 列表
    """

    __tablename__ = "test_projects"

    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="项目名称"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="项目描述"
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="项目负责人 ID"
    )
    # 关联文档 ID 列表（JSONB 存储 UUID 字符串列表）
    prd_doc_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="PRD 文档 ID 列表"
    )
    tech_doc_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="技术方案文档 ID 列表"
    )
    api_doc_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="接口文档 ID 列表"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", comment="状态: active/archived"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    requirements: Mapped[list["TestRequirement"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    test_plans: Mapped[list["TestPlan"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


# ======================================================================
# 需求点 — 从 PRD/UI 稿自动拆分的原子需求
# ======================================================================


class TestRequirement(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """需求点表 — 从 PRD/UI 稿自动提取的原子需求。

    一个需求点可生成多个测试用例。
    来源标识 source: ai_extract（AI提取）/ manual（手动创建）
    """

    __tablename__ = "test_requirements"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_projects.id"), nullable=False, comment="项目 ID"
    )
    # 需求来源文档（PRD 或 UI 稿的 Document ID）
    source_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="来源文档 ID（知识库 Document）"
    )
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="需求标题"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="需求详细描述"
    )
    # 需求分类: functional(功能) / non_functional(非功能) / ui(界面) / api(接口) / performance(性能)
    category: Mapped[str] = mapped_column(
        String(30), default="functional", comment="需求分类: functional/non_functional/ui/api/performance"
    )
    priority: Mapped[str] = mapped_column(
        String(20), default="normal", comment="优先级: low/normal/high/critical"
    )
    # 验收标准（JSONB 数组，每项为一个验收条件）
    acceptance_criteria: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="验收标准列表"
    )
    # AI 提取的原始文本片段
    source_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="AI 提取的原始文本片段"
    )
    source: Mapped[str] = mapped_column(
        String(20), default="ai_extract", comment="来源: ai_extract/manual"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="状态: pending/analyzed/generating_cases/cases_ready"
    )
    # 知识回流：变更线程 ID（追踪需求的版本演化，用于关联历史知识资产）
    change_thread_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="变更线程 ID（知识回流：追踪需求演化）"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    project: Mapped[TestProject] = relationship(back_populates="requirements")
    test_cases: Mapped[list["TestCase"]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan"
    )


# ======================================================================
# 测试用例 — 基于需求点 + 技术方案 + 接口文档生成
# ======================================================================


class TestCase(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """测试用例表 — 基于需求点和上下文文档生成的测试用例。

    生命周期：draft → pending_review → approved → active → deprecated
    created_by: ai_generate（AI生成）/ manual（手动创建）
    """

    __tablename__ = "test_cases"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_projects.id"), nullable=False, comment="项目 ID"
    )
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_requirements.id"), nullable=True, comment="关联需求 ID"
    )
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="用例标题"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="用例描述"
    )
    # 前置条件
    preconditions: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="前置条件"
    )
    # 测试步骤（JSONB 数组，每项含 step_no/action/expected）
    test_steps: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="测试步骤列表"
    )
    # 预期结果
    expected_result: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="预期结果"
    )
    # 测试类型: functional/api/ui/performance/security/compatibility
    test_type: Mapped[str] = mapped_column(
        String(30), default="functional", comment="测试类型: functional/api/ui/performance/security/compatibility"
    )
    priority: Mapped[str] = mapped_column(
        String(20), default="normal", comment="优先级: low/normal/high/critical"
    )
    # 用例状态
    status: Mapped[str] = mapped_column(
        String(20), default="draft", comment="状态: draft/pending_review/approved/active/deprecated"
    )
    # 标签
    tags: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="标签列表"
    )
    # 创建方式
    created_by: Mapped[str] = mapped_column(
        String(20), default="ai_generate", comment="创建方式: ai_generate/manual"
    )
    # AI 生成时引用的上下文文档 ID 列表
    context_doc_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="AI 生成时引用的上下文文档 ID 列表"
    )
    # 用例编号（项目内唯一）
    case_no: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="用例编号（如 TC-0001）"
    )
    # 知识回流：验证渠道（记录该用例通过哪些渠道验证过，如 ["api", "ui", "log"]）
    verification_channels: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="验证渠道列表（知识回流：多渠道验证记录）"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    requirement: Mapped[TestRequirement | None] = relationship(back_populates="test_cases")
    reviews: Mapped[list["TestReview"]] = relationship(
        back_populates="test_case", cascade="all, delete-orphan"
    )
    executions: Mapped[list["TestExecution"]] = relationship(
        back_populates="test_case", cascade="all, delete-orphan"
    )


# ======================================================================
# 用例评审 — 复用审核工作流模式
# ======================================================================


class TestReview(UUIDMixin, TimestampMixin, Base):
    """用例评审表 — 测试用例的评审记录。

    生命周期：pending → approved/rejected
    复用 AuditFlow 的 pending/approved/rejected 模式，
    但独立存储以支持测试特有的评审建议（suggestions）。
    """

    __tablename__ = "test_reviews"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_cases.id"), nullable=False, comment="用例 ID"
    )
    submitter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="提交者 ID"
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, comment="评审者 ID"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="状态: pending/approved/rejected"
    )
    # 评审意见
    comment: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="评审意见"
    )
    # AI 或人工评审建议（JSONB 数组，每项含 type/suggestion/severity）
    suggestions: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="评审建议列表"
    )
    # 评审结果摘要
    review_summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="评审结果摘要"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="评审处理时间"
    )

    test_case: Mapped["TestCase"] = relationship(back_populates="reviews")


# ======================================================================
# 测试计划 — 用例编排和执行调度
# ======================================================================


class TestPlan(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """测试计划表 — 用例编排和执行调度。

    AI 编排：根据用例类型、优先级、依赖关系，
    自动生成执行顺序和节点分配方案。
    """

    __tablename__ = "test_plans"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_projects.id"), nullable=False, comment="项目 ID"
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="计划名称"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="计划描述"
    )
    # 包含的用例 ID 列表
    case_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="包含的用例 ID 列表"
    )
    # 执行策略: sequential(顺序) / parallel(并行) / priority_based(按优先级)
    execution_strategy: Mapped[str] = mapped_column(
        String(30), default="priority_based", comment="执行策略: sequential/parallel/priority_based"
    )
    # AI 生成的编排方案（JSONB）
    # 含 execution_order(执行顺序) / node_assignments(节点分配) / dependencies(依赖关系)
    ai_orchestration: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="AI 编排方案"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="draft", comment="状态: draft/active/completed/archived"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="创建者 ID"
    )
    # 多租户隔离
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    project: Mapped[TestProject] = relationship(back_populates="test_plans")
    executions: Mapped[list["TestExecution"]] = relationship(
        back_populates="test_plan", cascade="all, delete-orphan"
    )


# ======================================================================
# 执行记录 — 用例执行结果
# ======================================================================


class TestExecution(UUIDMixin, TimestampMixin, Base):
    """执行记录表 — 用例在测试计划中的执行结果。

    executor: human(人工执行) / ai(AI自动执行)
    status: pending/running/passed/failed/blocked/skipped
    """

    __tablename__ = "test_executions"

    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_plans.id"), nullable=True, comment="测试计划 ID"
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_cases.id"), nullable=False, comment="用例 ID"
    )
    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, comment="执行人 ID（人工执行时）"
    )
    executor: Mapped[str] = mapped_column(
        String(20), default="human", comment="执行者: human/ai"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="状态: pending/running/passed/failed/blocked/skipped"
    )
    # 执行结果详情
    result: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="执行结果描述"
    )
    # 执行日志（JSONB，含步骤执行详情）
    execution_log: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="执行日志"
    )
    # 失败原因（failed/blocked 时填写）
    failure_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败原因"
    )
    # 执行耗时（秒）
    duration_seconds: Mapped[int] = mapped_column(
        Integer, default=0, comment="执行耗时（秒）"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始执行时间"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    # 知识回流：证据引用（截图、日志、构建产物等不可变证据的引用）
    evidence_ref: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="证据引用（知识回流：不可变证据快照）"
    )
    # 知识回流：回流状态（none/pending/processed，防止重复提取）
    compounding_status: Mapped[str] = mapped_column(
        String(20), default="none", comment="知识回流状态: none/pending/processed"
    )

    # 关系
    test_plan: Mapped[TestPlan | None] = relationship(back_populates="executions")
    test_case: Mapped[TestCase] = relationship(back_populates="executions")
