"""constraint channel: constraint_rules + constraint_audit_records + kb.category

Revision ID: c7d8e9f0a1b2
Revises: 6fff5feb64c4
Create Date: 2026-08-15

Phase 1（约束召回强化设计 §4 / §11.2）：
- constraint_rules：约束一等公民模型（非"带标签的文档"），
  trigger_entities GIN 检索（T2 实体触发器）、生效窗、版本链、
  软状态（禁 DELETE，retire 走 status）。
- constraint_audit_records：注入决策审计（injected / skipped_observe /
  filtered_perm），灰度期 observe-only 模式的数据基础。
- knowledge_bases.category：KB 领域属性（T4 高风险域默认注入的判定依据），
  存量 KB 为 NULL（不命中 T4，行为不变）。

PostgreSQL DDL（项目硬约束：无 SQLite 兼容代码）；GIN 与部分索引用原生
DDL（迁移库无 GIN 先例，参照 fb10a2c3d4e5 的 op.execute 范式）。
主键 UUID 由应用层生成（UUIDMixin default=uuid4），不依赖 pgcrypto。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "6fff5feb64c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS constraint_rules (
            id                      UUID PRIMARY KEY,
            tenant_id               UUID NULL,
            kb_id                   UUID NOT NULL REFERENCES knowledge_bases(id),
            document_id             UUID NOT NULL REFERENCES documents(id),
            chunk_id                VARCHAR(255) NOT NULL,
            scope                   VARCHAR(16) NOT NULL DEFAULT 'kb',
            rule_text               TEXT NOT NULL,
            normalized              JSONB NOT NULL,
            severity                VARCHAR(16) NOT NULL,
            actions                 VARCHAR(16)[] NOT NULL DEFAULT '{inject}',
            trigger_domains         VARCHAR(32)[] NOT NULL DEFAULT '{}',
            trigger_entities        TEXT[] NOT NULL DEFAULT '{}',
            trigger_intents         VARCHAR(32)[] NOT NULL DEFAULT '{}',
            effective_from          DATE NULL,
            effective_to            DATE NULL,
            version                 INT NOT NULL DEFAULT 1,
            superseded_by           UUID NULL REFERENCES constraint_rules(id),
            status                  VARCHAR(16) NOT NULL DEFAULT 'pending_review',
            classifier_confidence   FLOAT NOT NULL DEFAULT 0,
            reviewed_by             UUID NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 查询路径索引：主路径 (kb_id, status) 且仅 active/pending_review 行
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_constraint_rules_lookup
        ON constraint_rules (kb_id, status)
        WHERE status IN ('active', 'pending_review')
        """
    )
    # T2 实体触发器 — GIN 数组匹配（trigger_entities && :names）
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_constraint_rules_entities
        ON constraint_rules USING GIN (trigger_entities)
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_constraint_rules_doc ON constraint_rules (document_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS constraint_audit_records (
            id          UUID PRIMARY KEY,
            tenant_id   UUID NULL,
            session_id  VARCHAR(64) NOT NULL DEFAULT '',
            user_id     UUID NULL,
            query       TEXT NOT NULL DEFAULT '',
            kb_ids      JSONB NOT NULL DEFAULT '[]',
            rule_id     UUID NOT NULL REFERENCES constraint_rules(id),
            action      VARCHAR(32) NOT NULL,
            severity    VARCHAR(16) NOT NULL DEFAULT '',
            triggers    JSONB NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_constraint_audit_rule
        ON constraint_audit_records (rule_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_constraint_audit_session
        ON constraint_audit_records (session_id)
        """
    )

    # KB 领域属性 — T4 高风险域默认注入的判定依据（存量 NULL 不命中）
    op.execute(
        "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS category VARCHAR(32) NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS category")
    op.execute("DROP TABLE IF EXISTS constraint_audit_records")
    op.execute("DROP INDEX IF EXISTS ix_constraint_rules_doc")
    op.execute("DROP INDEX IF EXISTS ix_constraint_rules_entities")
    op.execute("DROP INDEX IF EXISTS ix_constraint_rules_lookup")
    op.execute("DROP TABLE IF EXISTS constraint_rules")
