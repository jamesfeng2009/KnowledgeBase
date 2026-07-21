"""
工具审批模型 — 单一职责：定义工具审批持久化表。

P1 核心表：当 DangerousToolGuard 拦截危险工具时，将审批请求持久化到此表，
支持服务重启后恢复未决审批，以及前端通过 REST 端点审批/拒绝。

设计决策：
    - agent_state_snapshot 使用 JSONB 存储 AgentState 快照（非 LangGraph Checkpointer），
      原因：企业知识库场景为短任务，不需要版本树/分支恢复，JSONB 足够且更简单。
    - tenant_id 预留多租户隔离字段（当前不实施过滤逻辑）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class ToolApproval(UUIDMixin, TimestampMixin, Base):
    """工具审批表 — 持久化危险工具调用的审批请求。

    生命周期：
        1. DangerousToolGuard 拦截危险工具 → 创建 pending 记录；
        2. 前端收到 approval_required SSE 事件 → 展示审批弹窗；
        3. 用户通过 REST 端点 approve/reject → 更新 status；
        4. 引擎轮询或 SSE 推送审批结果 → 恢复或终止 Agent Loop。
    """

    __tablename__ = "tool_approvals"

    # --- 关联 ---
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        comment="发起审批的用户 ID",
    )
    session_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="会话 ID（conversation_id 字符串形式）",
    )
    # 多租户预留 — 当前不实施隔离逻辑
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户预留）"
    )

    # --- 工具信息 ---
    tool_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="被拦截的工具名称"
    )
    tool_use_id: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="LLM 返回的 tool_use ID"
    )
    tool_arguments: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
        comment="工具调用参数（JSONB）",
    )
    reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
        comment="守卫拦截原因（展示给用户）",
    )
    irreversible: Mapped[bool] = mapped_column(
        nullable=False, default=False,
        comment="是否为不可逆操作",
    )

    # --- Agent 状态快照（JSONB） ---
    agent_state_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
        comment=(
            "AgentState 快照 — 审批通过后用于恢复 Agent Loop。"
            "包含 query/messages/retrieved_docs/tool_results/iteration 等。"
        ),
    )

    # --- 审批状态 ---
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True,
        comment="审批状态: pending/approved/rejected/expired",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="审批处理时间（approve/reject 时设置）",
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="审批处理人 ID（通常等于 user_id）",
    )
    expire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="审批过期时间（默认 1 小时后过期）",
    )
