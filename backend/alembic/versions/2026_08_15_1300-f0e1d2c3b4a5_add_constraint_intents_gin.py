"""constraint channel: GIN index for trigger_intents (T3 intent trigger)

Revision ID: f0e1d2c3b4a5
Revises: a1b2c3d4e5f7
Create Date: 2026-08-15

Phase 3 T3 意图触发接入（constraint-recall-design §6.2）：
- ix_constraint_rules_intents：GIN 数组匹配索引
  （trigger_intents && ARRAY['rag_search','RAG_SEARCH']），
  与 T1/T2 的 GIN 索引同范式。
- mandatory_keywords 的 rule_text ILIKE 路径不建索引（关键词来自
  用户查询、不可预知，表规模小；必要时后续 pg_trgm 再评估）。

纯索引迁移，无表结构变更 — PostgreSQL 原生 DDL。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f0e1d2c3b4a5"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # T3 意图触发器 — GIN 数组匹配（trigger_intents && :values）
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_constraint_rules_intents
        ON constraint_rules USING GIN (trigger_intents)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_constraint_rules_intents")
