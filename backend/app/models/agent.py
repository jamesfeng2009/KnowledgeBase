"""
Agent 配置模型 — 单一职责：定义 Agent 配置表，用于持久化自定义 Agent。

遵循单一职责：本模块仅定义 AgentConfig ORM 映射，不包含 Agent 执行逻辑。
Agent 的推理与工具调用由 Service 层编排，配置仅提供静态参数。
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class AgentConfig(UUIDMixin, TimestampMixin, Base):
    """Agent 配置表 — 持久化自定义 Agent 的参数与开关。

    字段说明：
    - type 标识 Agent 类型（qa / workflow / action），决定执行引擎路由；
    - config 为 JSONB，存储 Agent 专属配置（如 system_prompt、tools、model 等）；
    - is_enabled 控制是否在 Agent 列表中可见且可调用。
    """

    __tablename__ = "agent_configs"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=True,
        comment="租户 ID（私有部署可空）",
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Agent 名称"
    )
    type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Agent 类型: qa/workflow/action"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Agent 描述"
    )
    config: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="Agent 专属配置（system_prompt/tools/model 等）"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否启用"
    )
