"""
计费与租户模型 — 单一职责：定义租户、订阅、用量记录表。
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class Tenant(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """租户表 — SaaS 多租户。"""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="租户名称")
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="域名")
    plan: Mapped[str] = mapped_column(
        String(20), default="free", comment="套餐: free/pro/enterprise"
    )
    max_users: Mapped[int] = mapped_column(Integer, default=10, comment="最大用户数")
    max_storage: Mapped[int] = mapped_column(BigInteger, default=1073741824, comment="最大存储（字节）")
    settings: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="租户配置")
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="到期时间"
    )


class Subscription(UUIDMixin, TimestampMixin, Base):
    """订阅表 — 租户订阅套餐记录。

    P1-6: 补全计费所需字段（原模型字段过简，无法承载真实计费）。
    """

    __tablename__ = "subscriptions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, comment="租户 ID"
    )
    plan: Mapped[str] = mapped_column(String(20), comment="套餐: free/pro/enterprise")
    status: Mapped[str] = mapped_column(
        String(20), default="active", comment="状态: active/cancelled/expired/past_due"
    )
    billing_cycle: Mapped[str] = mapped_column(
        String(20), default="monthly", comment="计费周期: monthly/yearly"
    )
    seats: Mapped[int] = mapped_column(
        Integer, default=1, comment="席位数（用户数）"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="开始时间"
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="取消时间"
    )
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否自动续费"
    )
    price: Mapped[int] = mapped_column(Integer, default=0, comment="价格（分）")
    metadata_: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="订阅元数据（支付方式、优惠券等）"
    )


class UsageRecord(UUIDMixin, TimestampMixin, Base):
    """用量记录表 — 按 LLM 调用记录。"""

    __tablename__ = "usage_records"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, comment="租户 ID"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="用户 ID"
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False, comment="使用的模型")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输入 token")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输出 token")
    cost_cents: Mapped[int] = mapped_column(Integer, default=0, comment="成本（分）")
    request_type: Mapped[str] = mapped_column(
        String(30), default="chat", comment="请求类型: chat/embed/rerank/vision"
    )
    # P1-5: 请求耗时与状态字段（原 reports.py 用硬编码 1.5s 估算）
    duration_ms: Mapped[int] = mapped_column(
        Integer, default=0, comment="请求耗时（毫秒）"
    )
    success: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否成功"
    )
    request_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="请求追踪 ID"
    )
