"""
API 密钥模型 — 单一职责：定义 API 密钥表，用于程序化访问鉴权。

遵循单一职责：本模块仅定义 ApiKey ORM 映射，不包含密钥生成或校验逻辑。
密钥生成与哈希由 ApiKeyService 处理，保证模型层不感知安全细节。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class ApiKey(UUIDMixin, TimestampMixin, Base):
    """API 密钥表 — 用于程序化（非交互式）访问 API。

    安全说明：
    - key_hash 存储 SHA-256 哈希，明文密钥仅在创建时返回一次，不持久化；
    - key_prefix 存储明文前 8 位，用于列表展示识别；
    - scopes 为 TEXT[] 数组，限定该密钥可访问的资源范围；
    - is_active 标记停用状态，停用后鉴权直接拒绝。
    """

    __tablename__ = "api_keys"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=True,
        comment="租户 ID（私有部署可空）",
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="密钥名称（用户可读标识）"
    )
    key_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="SHA-256 密钥哈希"
    )
    key_prefix: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="密钥前缀（明文前 8 位，用于识别）"
    )
    scopes: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True, comment="授权范围列表"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="过期时间（空表示永不过期）"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最后使用时间"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否启用"
    )
