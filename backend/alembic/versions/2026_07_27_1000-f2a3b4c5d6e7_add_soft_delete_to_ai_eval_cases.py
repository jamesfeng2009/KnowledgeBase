"""add deleted_at to ai eval case tables (3 tables)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-27 10:00:00.000000

P1 修复 — ai_eval 用例级物理删除改软删除：

项目约束：不允许任何逻辑物理删除数据库数据。
此前 DocParseCase / RagEvalQuery / JudgeCase 三个模型缺少 SoftDeleteMixin，
对应 service 层 delete_case 直接 db.delete() 物理删除，违反约束。

本迁移为三张用例表补充 deleted_at 软删除列：
- ai_eval_doc_parse_cases
- ai_eval_rag_queries
- ai_eval_judge_cases

说明：
- 结果表（results）随用例查询过滤自然隐藏，不加 deleted_at；
  结果通过 case_id 关联，用例被软删后其结果不再被查询到。
- 数据集表（datasets）此前已有 deleted_at，无需变更。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "ai_eval_doc_parse_cases",
    "ai_eval_rag_queries",
    "ai_eval_judge_cases",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="软删除时间（NULL=未删除）",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "deleted_at")
