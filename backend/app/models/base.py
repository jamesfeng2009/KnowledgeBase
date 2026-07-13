"""
ORM 模型基类 — 单一职责：提供所有模型的公共字段。

遵循开闭原则：新增模型只需继承 Base，自动获得 id/created_at/updated_at。
遵循单一职责：基类只定义公共字段，不包含业务逻辑。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    pass


class TimestampMixin:
    """时间戳混入 — 提供 created_at 和 updated_at 字段。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UUIDMixin:
    """UUID 主键混入 — 所有业务表使用 UUID 主键，避免自增 ID 暴露数据量。"""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class SoftDeleteMixin:
    """软删除混入 — 不允许物理删除，统一使用 deleted_at 标记。"""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
