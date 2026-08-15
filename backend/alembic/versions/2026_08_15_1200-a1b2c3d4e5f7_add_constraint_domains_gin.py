"""constraint channel: GIN index for trigger_domains (T1 domain trigger)

Revision ID: a1b2c3d4e5f7
Revises: d8e9f0a1b2c3
Create Date: 2026-08-15

Phase 3 T1 域触发器接入（constraint-recall-design §6.1）：
- ix_constraint_rules_domains：GIN 数组匹配索引（trigger_domains && :domains），
  与 P1 的 ix_constraint_rules_entities（T2 实体触发）同范式。
- Router.distinct_domains 的 unnest 查询同样受益（元素级 GIN 加速）。

纯索引迁移，无表结构变更 — PostgreSQL 原生 DDL（项目硬约束：无 SQLite
兼容代码）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # T1 域触发器 — GIN 数组匹配（trigger_domains && :domains）
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_constraint_rules_domains
        ON constraint_rules USING GIN (trigger_domains)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_constraint_rules_domains")
