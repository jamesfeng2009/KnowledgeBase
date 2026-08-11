"""
Agent Event Log 模型 — 单一职责：LangGraph Agent Loop 事件流持久化。

定位：与 agent_checkpoints 互补的混合恢复机制。
- Checkpoint 保存状态快照（某一时刻的完整 state）
- EventLog 保存事件流（每次节点执行的增量变化）
- 混合恢复 = 加载最近 Checkpoint + 重放后续事件 = 任意时间点状态

使用场景：
    1. Checkpoint 损坏或丢失时，从更早的 Checkpoint + event log 重建
    2. 调试：回放某会话的完整事件流，定位问题节点
    3. 审计：保留事件流用于事后分析（含 latency / token / error）

注意：EventLogManager（app/memory/event_log.py）使用原生 SQL 操作此表，
本 ORM 模型主要用于 Alembic 迁移生成和 metadata 注册。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentEventLog(Base):
    """Agent Loop 事件日志表 — 与 agent_checkpoints 互补的混合恢复机制。

    每条记录对应一次节点执行（think/retrieve/tool_call/generate/reflect），
    保存节点输出的 state 增量，用于 Checkpoint 之外的细粒度恢复。
    """

    __tablename__ = "agent_event_logs"

    # 自增主键（BIGSERIAL）— 事件日志量大，用 bigint 而非 UUID
    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="事件自增 ID",
    )
    # 会话 ID（对应 Conversation ID 字符串形式）
    session_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="会话 ID（对应 Conversation ID）",
    )
    # 会话内自增序号 — 与 (session_id) 组合唯一，用于重放定位
    seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="会话内事件序号（从 1 开始递增）",
    )
    # 事件类型：node_start / node_end / state_update
    event_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="事件类型（node_start/node_end/state_update）",
    )
    # 节点名称：think / retrieve / tool_call / generate / reflect
    node_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Agent Loop 节点名",
    )
    # 当前迭代轮次
    iteration: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Agent Loop 迭代轮次",
    )
    # 节点输入摘要（脱敏后）
    input_data: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="节点输入摘要（已 PII 脱敏）",
    )
    # 节点输出（state 增量 dict，重放时按 LangGraph reducer 语义合并）
    output_data: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="节点输出 state 增量",
    )
    # 额外元数据（latency_ms / token_count / error 等）
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment="额外元数据（latency/token/error）",
    )
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
        nullable=False,
        comment="事件创建时间",
    )


# 表级约束： (session_id, seq) 唯一 — 通过 Alembic 迁移创建 UNIQUE INDEX
# 注意：metadata_ 字段在 Python 中用 `metadata_`（避免与 SQLAlchemy Base.metadata 冲突），
#       映射到数据库列名 `metadata`。
