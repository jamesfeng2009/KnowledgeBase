"""add ai eval injection test tables (3 tables)

Revision ID: f4d5e6a7b8c9
Revises: b8d2a0f3c5e7
Create Date: 2026-11-10 13:00:00.000000

补齐 Prompt Injection 防御测试缺失的 3 张表：

- ai_eval_injection_suites:   测试套件（一次完整防御测试运行的顶层容器）
- ai_eval_injection_cases:    攻击用例（preset 预置库 / custom 自定义）
- ai_eval_injection_results:  执行结果（pass/partial/fail 三档评分 + 5 维度检查）

背景（为什么必须补）：
    app/models/ai_eval.py 早已定义上述三个模型，app/api/v1/ai_eval.py 也已
    暴露 /api/v1/ai-eval/injection/* 系列端点，但从未写过迁移。测试用
    Base.metadata.create_all() 建表，所以单元测试全绿；而生产走
    AUTO_MIGRATE=true + AUTO_CREATE_TABLES=false，表根本不存在，
    端点一调用就是 UndefinedTable（500）。

表间依赖（创建顺序）：
    suites → cases → results（results 同时引用 cases 与 suites）

复用约定与其他 ai_eval 表一致：
    - UUID 主键（UUIDMixin）
    - created_at / updated_at（TimestampMixin，server_default=now()）
    - deleted_at 软删除（仅 suites，SoftDeleteMixin）
    - tenant_id 多租户隔离（仅 suites）
    - created_by 关联 users 表（仅 suites）
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4d5e6a7b8c9"
down_revision: Union[str, None] = "b8d2a0f3c5e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 Prompt Injection 防御测试的 3 张表。"""

    # ==================================================================
    # 1. ai_eval_injection_suites — 测试套件
    # ==================================================================
    op.create_table(
        "ai_eval_injection_suites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, comment="套件名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="套件描述"),
        sa.Column(
            "target_mode",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'system_prompt'"),
            comment="测试目标: system_prompt/full_rag",
        ),
        sa.Column(
            "kb_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="关联知识库 ID 列表（full_rag 模式）",
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
            "passed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="完美防御数",
        ),
        sa.Column(
            "partial_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="部分防御数",
        ),
        sa.Column(
            "failed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="防御失败数",
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
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        comment="Prompt Injection 测试套件",
    )
    op.create_index(
        "ix_ai_eval_injection_suites_tenant_id",
        "ai_eval_injection_suites",
        ["tenant_id"],
    )
    op.create_index(
        "ix_ai_eval_injection_suites_created_by",
        "ai_eval_injection_suites",
        ["created_by"],
    )

    # ==================================================================
    # 2. ai_eval_injection_cases — 攻击用例
    # ==================================================================
    op.create_table(
        "ai_eval_injection_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "suite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_injection_suites.id"),
            nullable=False,
            comment="所属套件 ID",
        ),
        sa.Column(
            "attack_type",
            sa.String(40),
            nullable=False,
            comment="攻击类型: basic_jailbreak/role_playing/system_override/"
            "context_poisoning/multi_turn/encoding_obfuscation/"
            "indirect_injection/combined",
        ),
        sa.Column(
            "severity",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'high'"),
            comment="严重程度: medium/high/critical",
        ),
        sa.Column("title", sa.String(500), nullable=False, comment="用例标题"),
        sa.Column("prompt", sa.Text(), nullable=False, comment="攻击提示词"),
        sa.Column(
            "expected_behavior",
            sa.Text(),
            nullable=False,
            comment="期望行为：模型应拒绝执行并给出安全提示",
        ),
        sa.Column(
            "attack_target",
            sa.Text(),
            nullable=True,
            comment="攻击目标关键词（用于检测模型是否泄露了敏感内容）",
        ),
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'preset'"),
            comment="来源: preset/custom",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Prompt Injection 攻击用例",
    )
    op.create_index(
        "ix_ai_eval_injection_cases_suite_id",
        "ai_eval_injection_cases",
        ["suite_id"],
    )

    # ==================================================================
    # 3. ai_eval_injection_results — 执行结果与评分
    # ==================================================================
    op.create_table(
        "ai_eval_injection_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_injection_cases.id"),
            nullable=False,
            comment="关联用例 ID",
        ),
        sa.Column(
            "suite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_eval_injection_suites.id"),
            nullable=False,
            comment="所属套件 ID",
        ),
        sa.Column(
            "response_text",
            sa.Text(),
            nullable=True,
            comment="模型响应文本",
        ),
        sa.Column(
            "verdict",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="评分等级: pass/partial/fail",
        ),
        sa.Column(
            "checks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="5 维度检查结果",
        ),
        sa.Column("score_reason", sa.Text(), nullable=True, comment="评分说明"),
        sa.Column(
            "response_time",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="响应耗时（秒）",
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
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Prompt Injection 执行结果",
    )
    op.create_index(
        "ix_ai_eval_injection_results_case_id",
        "ai_eval_injection_results",
        ["case_id"],
    )
    op.create_index(
        "ix_ai_eval_injection_results_suite_id",
        "ai_eval_injection_results",
        ["suite_id"],
    )


def downgrade() -> None:
    """回滚 Prompt Injection 测试 3 张表（FK 依赖逆序）。"""
    op.drop_index(
        "ix_ai_eval_injection_results_suite_id",
        table_name="ai_eval_injection_results",
    )
    op.drop_index(
        "ix_ai_eval_injection_results_case_id",
        table_name="ai_eval_injection_results",
    )
    op.drop_table("ai_eval_injection_results")

    op.drop_index(
        "ix_ai_eval_injection_cases_suite_id",
        table_name="ai_eval_injection_cases",
    )
    op.drop_table("ai_eval_injection_cases")

    op.drop_index(
        "ix_ai_eval_injection_suites_created_by",
        table_name="ai_eval_injection_suites",
    )
    op.drop_index(
        "ix_ai_eval_injection_suites_tenant_id",
        table_name="ai_eval_injection_suites",
    )
    op.drop_table("ai_eval_injection_suites")
