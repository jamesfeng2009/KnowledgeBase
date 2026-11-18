"""agent_checkpoints / agent_event_logs 补 tenant 列 + fail-closed RLS（B6）

Revision ID: 7a8b9c0d1e2f
Revises: e5f6a7b8c9d1
Create Date: 2026-11-18 15:00:00

## 改了什么

两表此前无 tenant 列无 RLS，租户隔离全靠上游归属校验
（chat_service 的 conversation.user_id 检查）。任何拿到别租户 session_id
的调用方直调 load_checkpoint / list_after 即可越权读 Agent 状态与事件流。

1. 两表补 ``tenant_id uuid``（可空，与 memory_facts 同款）；
2. 存量行按 ``session_id = conversations.id::text`` 回填租户；
   ``task:*`` 里程碑键无会话归属，留 NULL（milestone_runner 走 '*' 审计
   作用域读写，NULL 行在该作用域下可见，行为不变）；
3. ENABLE + FORCE ROW LEVEL SECURITY + fail-closed ``tenant_isolation``
   策略，谓词与 e8f9a0b1c2d3 完全一致（GUC 缺失拒绝所有行）。

写入侧（CheckpointManager.save_checkpoint / EventLogManager.append）
同批修复为显式填 tenant_id（构造参数优先，其次会话 GUC）。
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "7a8b9c0d1e2f"
down_revision = "e5f6a7b8c9d1"
branch_labels = None
depends_on = None


# 与 e8f9a0b1c2d3 的 _STRICT_PREDICATE 同语义（此处是直接 SQL，单引号不用双写）
_PREDICATE = (
    "current_setting('app.tenant_id', true) = '*' "
    "OR tenant_id::text = current_setting('app.tenant_id', true)"
)

_TABLES = ("agent_checkpoints", "agent_event_logs")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id uuid")
        op.execute(
            f"""
            UPDATE {table} t
            SET tenant_id = c.tenant_id
            FROM conversations c
            WHERE c.id::text = t.session_id
              AND t.tenant_id IS NULL
              AND c.tenant_id IS NOT NULL
            """
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS tenant_id")