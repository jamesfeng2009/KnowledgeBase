"""add doc_id and meta to qa_answers

Revision ID: ac1b2c3d4e6f
Revises: fb10a2c3d4e5
Create Date: 2026-08-08 12:00:00.000000

合成 QA 溯源：

为 qa_answers 表新增 doc_id 和 meta 两列（均 nullable，向后兼容）：
- doc_id：关联源文档（合成 QA 专用，真实回答保持 NULL）；
- meta：JSONB 扩展元数据，标注 source=synthetic / doc_id / category /
  classification 等，供 dataset_builder 幂等判断与密级继承使用。

幂等：ADD COLUMN IF NOT EXISTS，重复执行安全。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ac1b2c3d4e6f"
down_revision: Union[str, None] = "fb10a2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE qa_answers "
        "ADD COLUMN IF NOT EXISTS doc_id UUID REFERENCES documents(id)"
    )
    op.execute(
        "ALTER TABLE qa_answers ADD COLUMN IF NOT EXISTS meta JSONB"
    )
    # 溯源查询索引 — 幂等判断按 doc_id 检索已合成 QA
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_qa_answers_doc_id ON qa_answers (doc_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_qa_answers_doc_id")
    op.execute("ALTER TABLE qa_answers DROP COLUMN IF EXISTS meta")
    op.execute("ALTER TABLE qa_answers DROP COLUMN IF EXISTS doc_id")
