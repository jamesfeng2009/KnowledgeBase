"""P2 用户模型偏好 ORM 模型 — 单一职责：定义 user_model_preferences 表。

存储用户在特定会话中选择的模型，实现两级优先级：
    session 级（本表）> system 默认（models.json is_default）

设计决策：
- 每行对应一个 (user_id, session_id) 对，通过唯一约束防止重复
- session_id 使用字符串类型（兼容 conversation_id 的 UUID 字符串表示）
- model_id 引用 models.json 中的模型 ID，不使用 FK（模型配置在 JSON 而非 DB）
- 多租户预留 tenant_id 字段

遵循单一职责：本模块只定义表结构，不包含业务逻辑。
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class UserModelPreference(UUIDMixin, TimestampMixin, Base):
    """用户模型偏好表 — P2 会话级模型选择持久化。

    当用户在聊天界面切换模型时，记录到本表。
    下次对话时从本表读取，实现会话级模型记忆。

    两级优先级：
        1. session 级（本表）— 用户为该会话明确选择的模型
        2. system 默认 — models.json 中 is_default=True 的模型
    """

    __tablename__ = "user_model_preferences"
    __table_args__ = (
        # 每个 (user_id, session_id) 对只能有一条记录（upsert 语义）
        UniqueConstraint("user_id", "session_id", name="uq_user_session_model"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="用户 ID",
    )
    session_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="会话 ID（通常为 conversation_id 的字符串表示）",
    )
    model_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="模型 ID（引用 models.json 中的 id 字段）",
    )
    # 多租户预留 — 当前不实施隔离逻辑
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="租户 ID（多租户隔离预留）",
    )
