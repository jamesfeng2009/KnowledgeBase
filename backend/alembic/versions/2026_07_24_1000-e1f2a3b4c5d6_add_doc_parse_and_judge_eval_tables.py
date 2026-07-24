"""add doc parse and ai judge eval tables (6 tables)

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-07-24 10:00:00.000000

阶段三/四 AI 评测扩展 — 6 张新表：

文档解析评测（阶段三）：
- ai_eval_doc_parse_datasets:  解析评测数据集（顶层容器）
- ai_eval_doc_parse_cases:      解析用例（标注标准答案 + 解析结果/关联文档）
- ai_eval_doc_parse_results:    解析结果（文本/表格/公式/版面四维度指标）

AI Judge 自动评测（阶段四）：
- ai_eval_judge_datasets:       Judge 评测数据集（含评分维度配置）
- ai_eval_judge_cases:          Judge 用例（问题 + 参考答案 + 模型答案）
- ai_eval_judge_results:        Judge 结果（LLM 裁判多维评分 + 评语）

表间依赖（创建顺序）：
    datasets → cases → results

所有表复用：
    - UUID 主键（UUIDMixin）
    - created_at / updated_at 时间戳（TimestampMixin）
    - deleted_at 软删除（仅 datasets 表，cases/results 物理删除随父级 cascade）
    - tenant_id 多租户隔离（仅 datasets 表）
    - created_by 关联 users 表（仅 datasets 表）

指标体系参考 test.md：
    - 文档解析：编辑距离/CER + 表格匹配 + 公式匹配 + 版面还原（第六部分）
    - AI Judge：LLM 裁判多维评分（第七部分）
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建文档解析评测 + AI Judge 评测共 6 张表。"""

    # ==================================================================
    # 1. ai_eval_doc_parse_datasets — 文档解析评测数据集
    # ==================================================================
    op.create_table(
        "ai_eval_doc_parse_datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, comment="数据集名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="数据集描述"),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'created'"),
            comment="状态: created/running/completed/failed",
        ),
        sa.Column(
            "total_cases",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="用例总数",
        ),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="聚合解析质量指标",
        ),
        sa.Column(
            "duration_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="执行耗时（秒）",
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

    # ==================================================================
    # 2. ai_eval_doc_parse_cases — 文档解析用例
    # ==================================================================
    op.create_table(
        "ai_eval_doc_parse_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_doc_parse_datasets.id"),
            nullable=False,
            comment="所属数据集 ID",
        ),
        sa.Column("title", sa.String(500), nullable=False, comment="用例标题"),
        sa.Column(
            "doc_type",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pdf'"),
            comment="文档类型: pdf/docx/pptx/xlsx/html/image",
        ),
        sa.Column(
            "difficulty",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'medium'"),
            comment="难度: easy/medium/hard",
        ),
        sa.Column(
            "expected_text",
            sa.Text(),
            nullable=False,
            comment="人工标注的标准答案文本",
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="关联文档 ID（Docling 端到端模式）",
        ),
        sa.Column(
            "parsed_text",
            sa.Text(),
            nullable=True,
            comment="待评测的解析结果文本（直接提供模式）",
        ),
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'custom'"),
            comment="来源: preset/custom",
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
        "ix_ai_eval_doc_parse_cases_dataset_id",
        "ai_eval_doc_parse_cases",
        ["dataset_id"],
    )

    # ==================================================================
    # 3. ai_eval_doc_parse_results — 文档解析结果
    # ==================================================================
    op.create_table(
        "ai_eval_doc_parse_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_doc_parse_cases.id"),
            nullable=False,
            comment="关联用例 ID",
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_doc_parse_datasets.id"),
            nullable=False,
            comment="所属数据集 ID",
        ),
        sa.Column(
            "parsed_text",
            sa.Text(),
            nullable=True,
            comment="实际解析结果文本",
        ),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="解析质量指标",
        ),
        sa.Column(
            "overall_score",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="综合得分（0-100）",
        ),
        sa.Column(
            "parse_time_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="解析耗时（毫秒）",
        ),
        sa.Column(
            "used_docling",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否使用 Docling 端到端解析",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="执行错误信息",
        ),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="执行时间",
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
        "ix_ai_eval_doc_parse_results_case_id",
        "ai_eval_doc_parse_results",
        ["case_id"],
    )

    # ==================================================================
    # 4. ai_eval_judge_datasets — AI Judge 评测数据集
    # ==================================================================
    op.create_table(
        "ai_eval_judge_datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, comment="数据集名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="数据集描述"),
        sa.Column(
            "judge_model",
            sa.String(100),
            nullable=False,
            server_default=sa.text("'default'"),
            comment="裁判模型标识",
        ),
        sa.Column(
            "dimensions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="评分维度列表",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'created'"),
            comment="状态: created/running/completed/failed",
        ),
        sa.Column(
            "total_cases",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="用例总数",
        ),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="聚合裁判评分",
        ),
        sa.Column(
            "duration_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="执行耗时（秒）",
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

    # ==================================================================
    # 5. ai_eval_judge_cases — AI Judge 用例
    # ==================================================================
    op.create_table(
        "ai_eval_judge_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_judge_datasets.id"),
            nullable=False,
            comment="所属数据集 ID",
        ),
        sa.Column("question", sa.Text(), nullable=False, comment="问题/指令"),
        sa.Column(
            "reference_answer",
            sa.Text(),
            nullable=False,
            comment="参考答案（标准答案）",
        ),
        sa.Column(
            "model_answer",
            sa.Text(),
            nullable=False,
            comment="待评测的模型答案",
        ),
        sa.Column(
            "category",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'qa'"),
            comment="场景类别: instruction/qa/reasoning/code/roleplay/safety",
        ),
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'custom'"),
            comment="来源: preset/custom",
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
        "ix_ai_eval_judge_cases_dataset_id",
        "ai_eval_judge_cases",
        ["dataset_id"],
    )

    # ==================================================================
    # 6. ai_eval_judge_results — AI Judge 结果
    # ==================================================================
    op.create_table(
        "ai_eval_judge_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_judge_cases.id"),
            nullable=False,
            comment="关联用例 ID",
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_judge_datasets.id"),
            nullable=False,
            comment="所属数据集 ID",
        ),
        sa.Column(
            "scores",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="各维度评分（0-100）",
        ),
        sa.Column(
            "overall_score",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="综合得分（0-100）",
        ),
        sa.Column(
            "reasoning",
            sa.Text(),
            nullable=True,
            comment="裁判评语/理由",
        ),
        sa.Column(
            "raw_response",
            sa.Text(),
            nullable=True,
            comment="裁判原始响应",
        ),
        sa.Column(
            "judge_model",
            sa.String(100),
            nullable=True,
            comment="实际使用的裁判模型",
        ),
        sa.Column(
            "response_time_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="响应耗时（毫秒）",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="执行错误信息",
        ),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="执行时间",
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
        "ix_ai_eval_judge_results_case_id",
        "ai_eval_judge_results",
        ["case_id"],
    )


def downgrade() -> None:
    """回滚文档解析 + AI Judge 评测迁移。"""
    # Judge 结果/用例/数据集
    op.drop_index("ix_ai_eval_judge_results_case_id", table_name="ai_eval_judge_results")
    op.drop_table("ai_eval_judge_results")
    op.drop_index("ix_ai_eval_judge_cases_dataset_id", table_name="ai_eval_judge_cases")
    op.drop_table("ai_eval_judge_cases")
    op.drop_table("ai_eval_judge_datasets")

    # DocParse 结果/用例/数据集
    op.drop_index("ix_ai_eval_doc_parse_results_case_id", table_name="ai_eval_doc_parse_results")
    op.drop_table("ai_eval_doc_parse_results")
    op.drop_index("ix_ai_eval_doc_parse_cases_dataset_id", table_name="ai_eval_doc_parse_cases")
    op.drop_table("ai_eval_doc_parse_cases")
    op.drop_table("ai_eval_doc_parse_datasets")
