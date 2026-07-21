"""add testing platform tables (6 tables)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-21 12:00:00.000000

智能测试平台 — 6 张表：
- test_projects:       测试项目（关联 PRD/技术方案/接口文档）
- test_requirements:   需求点（从 PRD 自动拆分）
- test_cases:          测试用例（AI 生成或手动创建）
- test_reviews:        用例评审记录
- test_plans:          测试计划（含 AI 编排方案）
- test_executions:     执行记录

表间依赖（创建顺序）：
    test_projects → test_requirements → test_cases
    test_cases → test_reviews
    test_projects → test_plans → test_executions
    test_cases → test_executions

所有表复用：
    - UUID 主键（UUIDMixin）
    - created_at / updated_at 时间戳（TimestampMixin）
    - deleted_at 软删除（SoftDeleteMixin，test_reviews/test_executions 除外）
    - tenant_id 多租户隔离（test_projects/test_requirements/test_cases/test_plans）
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建智能测试平台 6 张表。"""

    # ==================================================================
    # 1. test_projects — 测试项目
    # ==================================================================
    op.create_table(
        "test_projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, comment="项目名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="项目描述"),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
            comment="项目负责人 ID",
        ),
        sa.Column(
            "prd_doc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="PRD 文档 ID 列表",
        ),
        sa.Column(
            "tech_doc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="技术方案文档 ID 列表",
        ),
        sa.Column(
            "api_doc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="接口文档 ID 列表",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'active'"),
            comment="状态: active/archived",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_test_projects_owner_id", "test_projects", ["owner_id"])
    op.create_index("ix_test_projects_tenant_id", "test_projects", ["tenant_id"])
    op.create_index("ix_test_projects_status", "test_projects", ["status"])

    # ==================================================================
    # 2. test_requirements — 需求点
    # ==================================================================
    op.create_table(
        "test_requirements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_projects.id"),
            nullable=False,
            comment="项目 ID",
        ),
        sa.Column(
            "source_doc_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="来源文档 ID（知识库 Document）",
        ),
        sa.Column("title", sa.String(500), nullable=False, comment="需求标题"),
        sa.Column("description", sa.Text(), nullable=True, comment="需求详细描述"),
        sa.Column(
            "category",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'functional'"),
            comment="需求分类: functional/non_functional/ui/api/performance",
        ),
        sa.Column(
            "priority",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'normal'"),
            comment="优先级: low/normal/high/critical",
        ),
        sa.Column(
            "acceptance_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="验收标准列表",
        ),
        sa.Column(
            "source_text",
            sa.Text(),
            nullable=True,
            comment="AI 提取的原始文本片段",
        ),
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'ai_extract'"),
            comment="来源: ai_extract/manual",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="状态: pending/analyzed/generating_cases/cases_ready",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_test_requirements_project_id", "test_requirements", ["project_id"]
    )
    op.create_index(
        "ix_test_requirements_source_doc_id",
        "test_requirements",
        ["source_doc_id"],
    )
    op.create_index(
        "ix_test_requirements_status", "test_requirements", ["status"]
    )
    op.create_index(
        "ix_test_requirements_tenant_id", "test_requirements", ["tenant_id"]
    )

    # ==================================================================
    # 3. test_cases — 测试用例
    # ==================================================================
    op.create_table(
        "test_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_projects.id"),
            nullable=False,
            comment="项目 ID",
        ),
        sa.Column(
            "requirement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_requirements.id"),
            nullable=True,
            comment="关联需求 ID",
        ),
        sa.Column("title", sa.String(500), nullable=False, comment="用例标题"),
        sa.Column("description", sa.Text(), nullable=True, comment="用例描述"),
        sa.Column("preconditions", sa.Text(), nullable=True, comment="前置条件"),
        sa.Column(
            "test_steps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="测试步骤列表",
        ),
        sa.Column(
            "expected_result", sa.Text(), nullable=True, comment="预期结果"
        ),
        sa.Column(
            "test_type",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'functional'"),
            comment="测试类型: functional/api/ui/performance/security/compatibility",
        ),
        sa.Column(
            "priority",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'normal'"),
            comment="优先级: low/normal/high/critical",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'draft'"),
            comment="状态: draft/pending_review/approved/active/deprecated",
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="标签列表",
        ),
        sa.Column(
            "created_by",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'ai_generate'"),
            comment="创建方式: ai_generate/manual",
        ),
        sa.Column(
            "context_doc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="AI 生成时引用的上下文文档 ID 列表",
        ),
        sa.Column(
            "case_no",
            sa.String(50),
            nullable=True,
            comment="用例编号（如 TC-0001）",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_test_cases_project_id", "test_cases", ["project_id"])
    op.create_index(
        "ix_test_cases_requirement_id", "test_cases", ["requirement_id"]
    )
    op.create_index("ix_test_cases_status", "test_cases", ["status"])
    op.create_index("ix_test_cases_test_type", "test_cases", ["test_type"])
    op.create_index("ix_test_cases_case_no", "test_cases", ["case_no"])
    op.create_index("ix_test_cases_tenant_id", "test_cases", ["tenant_id"])

    # ==================================================================
    # 4. test_reviews — 用例评审
    # ==================================================================
    op.create_table(
        "test_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_cases.id"),
            nullable=False,
            comment="用例 ID",
        ),
        sa.Column(
            "submitter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
            comment="提交者 ID",
        ),
        sa.Column(
            "reviewer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
            comment="评审者 ID",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="状态: pending/approved/rejected",
        ),
        sa.Column("comment", sa.Text(), nullable=True, comment="评审意见"),
        sa.Column(
            "suggestions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="评审建议列表",
        ),
        sa.Column(
            "review_summary", sa.Text(), nullable=True, comment="评审结果摘要"
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="评审处理时间",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_test_reviews_case_id", "test_reviews", ["case_id"])
    op.create_index(
        "ix_test_reviews_submitter_id", "test_reviews", ["submitter_id"]
    )
    op.create_index("ix_test_reviews_status", "test_reviews", ["status"])

    # ==================================================================
    # 5. test_plans — 测试计划
    # ==================================================================
    op.create_table(
        "test_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_projects.id"),
            nullable=False,
            comment="项目 ID",
        ),
        sa.Column("name", sa.String(255), nullable=False, comment="计划名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="计划描述"),
        sa.Column(
            "case_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="包含的用例 ID 列表",
        ),
        sa.Column(
            "execution_strategy",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'priority_based'"),
            comment="执行策略: sequential/parallel/priority_based",
        ),
        sa.Column(
            "ai_orchestration",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="AI 编排方案",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'draft'"),
            comment="状态: draft/active/completed/archived",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
            comment="创建者 ID",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_test_plans_project_id", "test_plans", ["project_id"])
    op.create_index("ix_test_plans_status", "test_plans", ["status"])
    op.create_index("ix_test_plans_created_by", "test_plans", ["created_by"])
    op.create_index("ix_test_plans_tenant_id", "test_plans", ["tenant_id"])

    # ==================================================================
    # 6. test_executions — 执行记录
    # ==================================================================
    op.create_table(
        "test_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_plans.id"),
            nullable=True,
            comment="测试计划 ID",
        ),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_cases.id"),
            nullable=False,
            comment="用例 ID",
        ),
        sa.Column(
            "executor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
            comment="执行人 ID（人工执行时）",
        ),
        sa.Column(
            "executor",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'human'"),
            comment="执行者: human/ai",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="状态: pending/running/passed/failed/blocked/skipped",
        ),
        sa.Column("result", sa.Text(), nullable=True, comment="执行结果描述"),
        sa.Column(
            "execution_log",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="执行日志",
        ),
        sa.Column(
            "failure_reason", sa.Text(), nullable=True, comment="失败原因"
        ),
        sa.Column(
            "duration_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="执行耗时（秒）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="开始执行时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="完成时间",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_test_executions_plan_id", "test_executions", ["plan_id"]
    )
    op.create_index(
        "ix_test_executions_case_id", "test_executions", ["case_id"]
    )
    op.create_index(
        "ix_test_executions_executor_id", "test_executions", ["executor_id"]
    )
    op.create_index(
        "ix_test_executions_status", "test_executions", ["status"]
    )


def downgrade() -> None:
    """删除智能测试平台 6 张表（逆序删除以解除外键依赖）。"""

    # 6. test_executions
    op.drop_index("ix_test_executions_status", table_name="test_executions")
    op.drop_index(
        "ix_test_executions_executor_id", table_name="test_executions"
    )
    op.drop_index("ix_test_executions_case_id", table_name="test_executions")
    op.drop_index("ix_test_executions_plan_id", table_name="test_executions")
    op.drop_table("test_executions")

    # 5. test_plans
    op.drop_index("ix_test_plans_tenant_id", table_name="test_plans")
    op.drop_index("ix_test_plans_created_by", table_name="test_plans")
    op.drop_index("ix_test_plans_status", table_name="test_plans")
    op.drop_index("ix_test_plans_project_id", table_name="test_plans")
    op.drop_table("test_plans")

    # 4. test_reviews
    op.drop_index("ix_test_reviews_status", table_name="test_reviews")
    op.drop_index("ix_test_reviews_submitter_id", table_name="test_reviews")
    op.drop_index("ix_test_reviews_case_id", table_name="test_reviews")
    op.drop_table("test_reviews")

    # 3. test_cases
    op.drop_index("ix_test_cases_tenant_id", table_name="test_cases")
    op.drop_index("ix_test_cases_case_no", table_name="test_cases")
    op.drop_index("ix_test_cases_test_type", table_name="test_cases")
    op.drop_index("ix_test_cases_status", table_name="test_cases")
    op.drop_index("ix_test_cases_requirement_id", table_name="test_cases")
    op.drop_index("ix_test_cases_project_id", table_name="test_cases")
    op.drop_table("test_cases")

    # 2. test_requirements
    op.drop_index(
        "ix_test_requirements_tenant_id", table_name="test_requirements"
    )
    op.drop_index(
        "ix_test_requirements_status", table_name="test_requirements"
    )
    op.drop_index(
        "ix_test_requirements_source_doc_id", table_name="test_requirements"
    )
    op.drop_index(
        "ix_test_requirements_project_id", table_name="test_requirements"
    )
    op.drop_table("test_requirements")

    # 1. test_projects
    op.drop_index("ix_test_projects_status", table_name="test_projects")
    op.drop_index("ix_test_projects_tenant_id", table_name="test_projects")
    op.drop_index("ix_test_projects_owner_id", table_name="test_projects")
    op.drop_table("test_projects")
