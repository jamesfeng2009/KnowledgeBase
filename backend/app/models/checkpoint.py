"""
Agent Checkpoint 模型 — 单一职责：LangGraph Agent Loop 状态持久化。

定位：多轮对话中断恢复、Agent Loop 迭代状态追踪。
特点：基于 PostgreSQL 的 upsert，支持会话状态恢复。

注意：CheckpointManager（app/memory/checkpoint.py）使用原生 SQL 操作此表，
本 ORM 模型主要用于 Alembic 迁移生成和 metadata 注册。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentCheckpoint(Base):
    """Agent Loop 状态检查点表。

    存储 Agent Loop 的中间状态，支持多轮对话中断恢复。
    每个 session_id 唯一对应一条记录（upsert 语义）。
    """

    __tablename__ = "agent_checkpoints"

    # session_id 对应 Conversation.id（字符串形式）
    # 作为主键，唯一标识一个会话的检查点
    session_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="会话 ID（对应 Conversation ID）"
    )
    # Agent 完整状态（messages, retrieved_docs, tool_results 等）
    agent_state: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="Agent 完整状态 JSON"
    )
    # 当前迭代次数
    iteration: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="当前迭代次数"
    )
    # 更新时间（每次 save_checkpoint 时更新）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
        onupdate=text("NOW()"),
        nullable=False,
        comment="最后更新时间",
    )
