"""add agent_event_logs table

Revision ID: bd2c3d4e5f70
Revises: ac1b2c3d4e6f
Create Date: 2026-08-11 10:00:00.000000

P2-7 混合恢复 — Agent Loop 事件日志表：

与 agent_checkpoints 互补的状态恢复机制：
- Checkpoint 保存状态快照（基线）
- EventLog 保存事件流（增量）
- 混合恢复 = 加载 Checkpoint + 重放后续事件 = 任意时间点状态

使用场景：
    1. Checkpoint 损坏或丢失时，从更早 Checkpoint + event log 重建
    2. 调试：回放会话完整事件流，定位问题节点
    3. 审计：保留事件流用于事后分析

幂等：CREATE TABLE IF NOT EXISTS，重复执行安全。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "bd2c3d4e5f70"
down_revision: Union[str, None] = "ac1b2c3d4e6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_event_logs (
            id BIGSERIAL PRIMARY KEY,
            session_id VARCHAR(64) NOT NULL,
            seq INTEGER NOT NULL,
            event_type VARCHAR(32) NOT NULL,
            node_name VARCHAR(64) NOT NULL,
            iteration INTEGER NOT NULL DEFAULT 0,
            input_data JSONB,
            output_data JSONB,
            metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # 会话内 seq 唯一 — 重放定位的基准
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_event_logs_session_seq "
        "ON agent_event_logs (session_id, seq)"
    )
    # 按 session_id 查询事件流的主索引
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_event_logs_session_id "
        "ON agent_event_logs (session_id)"
    )
    # 按 created_at 清理旧事件的辅助索引
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_event_logs_created_at "
        "ON agent_event_logs (created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_event_logs_created_at")
    op.execute("DROP INDEX IF EXISTS ix_agent_event_logs_session_id")
    op.execute("DROP INDEX IF EXISTS ux_agent_event_logs_session_seq")
    op.execute("DROP TABLE IF EXISTS agent_event_logs")
