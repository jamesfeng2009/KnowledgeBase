"""constraint classifier phase 2: documents.doc_role + constraint review fields

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-15

Phase 2（约束召回强化设计 §4.2 / §5）：
- documents.doc_role：文档级粗标（normal | constraint_source），供运营检索
  与日志标注；写入向量索引（五处透传）。注意：doc_role 不用于必召回 —
  必召回走 constraint_rules 表（确定域），与向量检索（概率域）物理隔离。
- constraint_rules.reviewed_at / review_comment：人审闭环记录（仿
  high_risk_audit_records 的复查三字段），approve → active / reject →
  retired；reviewed_at 与 superseded_by 区分「人审退休」与「版本链退休」，
  供误判率统计（get_review_stats）反哺 CONSTRAINT_AUTO_CONFIDENCE。

PostgreSQL DDL（项目硬约束：无 SQLite 兼容代码）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 文档级粗标 — 存量 'normal'（普通文档），抽到约束条款的文档置
    # constraint_source（extract_constraints 写回）
    op.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_role VARCHAR(16) "
        "NOT NULL DEFAULT 'normal'"
    )

    # 人审记录字段 — 与 reviewed_by 配套（Phase 1 已建）
    op.execute(
        "ALTER TABLE constraint_rules ADD COLUMN IF NOT EXISTS reviewed_at "
        "TIMESTAMPTZ NULL"
    )
    op.execute(
        "ALTER TABLE constraint_rules ADD COLUMN IF NOT EXISTS review_comment "
        "TEXT NULL"
    )
    # 人审队列查询：pending_review 列表按 KB 过滤（管理台分页）
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_constraint_rules_review "
        "ON constraint_rules (status, kb_id) "
        "WHERE status = 'pending_review'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_constraint_rules_review")
    op.execute("ALTER TABLE constraint_rules DROP COLUMN IF EXISTS review_comment")
    op.execute("ALTER TABLE constraint_rules DROP COLUMN IF EXISTS reviewed_at")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS doc_role")
