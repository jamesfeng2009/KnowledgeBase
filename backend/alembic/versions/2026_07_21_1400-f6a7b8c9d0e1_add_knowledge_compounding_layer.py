"""add knowledge compounding layer tables and testing model fields

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-21 14:00:00.000000

知识回流层 — 3 张新表 + 3 个测试模型新增字段：

新表：
- knowledge_assets:    知识资产（4 类：缺陷经验/回归SOP/图谱关联/验证基线）
- compounding_tasks:   回流任务（跟踪异步知识提取过程）
- knowledge_conflicts: 知识冲突（检测到的新旧知识冲突记录）

测试模型新增字段（ALTER TABLE）：
- test_requirements.change_thread_id:  变更线程 ID（追踪需求演化）
- test_cases.verification_channels:     验证渠道列表（多渠道验证记录）
- test_executions.evidence_ref:         证据引用（不可变证据快照）
- test_executions.compounding_status:   回流状态（none/pending/processed）

表间依赖（创建顺序）：
    compounding_tasks → knowledge_assets → knowledge_conflicts
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建知识回流层 3 张表 + 测试模型新增字段。"""

    # ==================================================================
    # 1. 测试模型新增字段（ALTER TABLE）
    # ==================================================================

    # test_requirements: 变更线程 ID
    op.add_column(
        "test_requirements",
        sa.Column(
            "change_thread_id",
            sa.String(100),
            nullable=True,
            comment="变更线程 ID（知识回流：追踪需求演化）",
        ),
    )

    # test_cases: 验证渠道
    op.add_column(
        "test_cases",
        sa.Column(
            "verification_channels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="验证渠道列表（知识回流：多渠道验证记录）",
        ),
    )

    # test_executions: 证据引用 + 回流状态
    op.add_column(
        "test_executions",
        sa.Column(
            "evidence_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="证据引用（知识回流：不可变证据快照）",
        ),
    )
    op.add_column(
        "test_executions",
        sa.Column(
            "compounding_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'none'"),
            comment="知识回流状态: none/pending/processed",
        ),
    )
    op.create_index(
        "ix_test_executions_compounding_status",
        "test_executions",
        ["compounding_status"],
    )

    # ==================================================================
    # 2. compounding_tasks — 回流任务表
    # ==================================================================
    op.create_table(
        "compounding_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_executions.id"),
            nullable=True,
            comment="执行记录 ID",
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_projects.id"),
            nullable=True,
            comment="测试项目 ID",
        ),
        sa.Column(
            "task_type",
            sa.String(30),
            nullable=False,
            comment="任务类型: extraction/conflict_detection/reuse_injection",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="状态: pending/running/completed/failed/skipped",
        ),
        sa.Column(
            "trigger_source",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'execution_completed'"),
            comment="触发来源: execution_completed/manual/scheduled",
        ),
        sa.Column(
            "extracted_asset_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="提取的资产 ID 列表",
        ),
        sa.Column(
            "conflicts_detected",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="检测到的冲突数量",
        ),
        sa.Column(
            "assets_injected",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="注入的历史资产数量",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="错误信息",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="开始时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="完成时间",
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
    )

    # 索引
    op.create_index(
        "ix_compounding_tasks_task_type",
        "compounding_tasks",
        ["task_type"],
    )
    op.create_index(
        "ix_compounding_tasks_status",
        "compounding_tasks",
        ["status"],
    )
    op.create_index(
        "ix_compounding_tasks_execution_id",
        "compounding_tasks",
        ["execution_id"],
    )
    op.create_index(
        "ix_compounding_tasks_project_id",
        "compounding_tasks",
        ["project_id"],
    )

    # ==================================================================
    # 3. knowledge_assets — 知识资产表
    # ==================================================================
    op.create_table(
        "knowledge_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_type",
            sa.String(30),
            nullable=False,
            comment="资产类型: defect_experience/regression_sop/graph_association/verification_baseline",
        ),
        sa.Column(
            "source_type",
            sa.String(30),
            nullable=False,
            comment="来源类型: test_execution/test_case/test_requirement/manual",
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="来源实体 ID",
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("test_projects.id"),
            nullable=True,
            comment="测试项目 ID",
        ),
        sa.Column(
            "title",
            sa.String(500),
            nullable=False,
            comment="资产标题",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="知识内容（自然语言描述）",
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="AI 生成的摘要",
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="标签列表",
        ),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="沉淀的文档 ID（知识库 Document）",
        ),
        sa.Column(
            "graph_nodes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="图谱节点列表（graph_association 类型）",
        ),
        sa.Column(
            "graph_relationships",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="图谱关系列表（graph_association 类型）",
        ),
        sa.Column(
            "graphiti_entity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Graphiti 实体 ID（verification_baseline 类型）",
        ),
        sa.Column(
            "confidence_score",
            sa.Float(),
            nullable=True,
            comment="AI 置信度（0.0~1.0）",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'draft'"),
            comment="状态: draft/active/deprecated/conflict",
        ),
        sa.Column(
            "conflict_with",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="冲突的资产 ID 列表",
        ),
        sa.Column(
            "compounding_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("compounding_tasks.id"),
            nullable=True,
            comment="回流任务 ID",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="租户 ID（多租户隔离）",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间",
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

    # 索引
    op.create_index(
        "ix_knowledge_assets_asset_type",
        "knowledge_assets",
        ["asset_type"],
    )
    op.create_index(
        "ix_knowledge_assets_status",
        "knowledge_assets",
        ["status"],
    )
    op.create_index(
        "ix_knowledge_assets_source_id",
        "knowledge_assets",
        ["source_id"],
    )
    op.create_index(
        "ix_knowledge_assets_project_id",
        "knowledge_assets",
        ["project_id"],
    )
    op.create_index(
        "ix_knowledge_assets_deleted_at",
        "knowledge_assets",
        ["deleted_at"],
    )
    # 复合索引：类型 + 状态（查询同类型 active 资产）
    op.create_index(
        "ix_knowledge_assets_type_status",
        "knowledge_assets",
        ["asset_type", "status"],
    )

    # ==================================================================
    # 4. knowledge_conflicts — 知识冲突表
    # ==================================================================
    op.create_table(
        "knowledge_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "new_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_assets.id"),
            nullable=False,
            comment="新资产 ID",
        ),
        sa.Column(
            "existing_asset_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="已有资产 ID",
        ),
        sa.Column(
            "conflict_type",
            sa.String(20),
            nullable=False,
            comment="冲突类型: contradiction/supersede/overlap",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="冲突描述",
        ),
        sa.Column(
            "resolution",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="解决方案: new_wins/existing_wins/merged/pending",
        ),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="处理人 ID",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="处理时间",
        ),
        sa.Column(
            "resolution_note",
            sa.Text(),
            nullable=True,
            comment="解决备注",
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
    )

    # 索引
    op.create_index(
        "ix_knowledge_conflicts_new_asset_id",
        "knowledge_conflicts",
        ["new_asset_id"],
    )
    op.create_index(
        "ix_knowledge_conflicts_existing_asset_id",
        "knowledge_conflicts",
        ["existing_asset_id"],
    )
    op.create_index(
        "ix_knowledge_conflicts_resolution",
        "knowledge_conflicts",
        ["resolution"],
    )


def downgrade() -> None:
    """回滚知识回流层迁移。"""
    # 删除冲突表
    op.drop_index("ix_knowledge_conflicts_resolution", table_name="knowledge_conflicts")
    op.drop_index("ix_knowledge_conflicts_existing_asset_id", table_name="knowledge_conflicts")
    op.drop_index("ix_knowledge_conflicts_new_asset_id", table_name="knowledge_conflicts")
    op.drop_table("knowledge_conflicts")

    # 删除资产表
    op.drop_index("ix_knowledge_assets_type_status", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_deleted_at", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_project_id", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_source_id", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_status", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_asset_type", table_name="knowledge_assets")
    op.drop_table("knowledge_assets")

    # 删除任务表
    op.drop_index("ix_compounding_tasks_project_id", table_name="compounding_tasks")
    op.drop_index("ix_compounding_tasks_execution_id", table_name="compounding_tasks")
    op.drop_index("ix_compounding_tasks_status", table_name="compounding_tasks")
    op.drop_index("ix_compounding_tasks_task_type", table_name="compounding_tasks")
    op.drop_table("compounding_tasks")

    # 回滚测试模型新增字段
    op.drop_index("ix_test_executions_compounding_status", table_name="test_executions")
    op.drop_column("test_executions", "compounding_status")
    op.drop_column("test_executions", "evidence_ref")
    op.drop_column("test_cases", "verification_channels")
    op.drop_column("test_requirements", "change_thread_id")
