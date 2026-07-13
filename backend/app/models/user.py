"""
用户与权限模型 — 单一职责：定义用户、角色、部门、知识库成员表。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


class Department(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """部门表 — 从 LDAP 同步的组织架构。"""

    __tablename__ = "departments"

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True, comment="父部门 ID"
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="部门名称")
    ldap_dn: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="LDAP DN")
    sort_order: Mapped[int] = mapped_column(default=0, comment="排序")

    children: Mapped[list["Department"]] = relationship(
        back_populates="parent"
    )
    parent: Mapped["Department | None"] = relationship(
        back_populates="children", remote_side="Department.id"
    )


class User(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """用户表。"""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, comment="邮箱")
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False, comment="密码哈希")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="姓名")
    avatar: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="头像 URL")
    role: Mapped[str] = mapped_column(
        String(20), default="viewer", comment="角色: admin/kb_admin/editor/viewer"
    )
    dept_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True, comment="部门 ID"
    )
    clearance_level: Mapped[str] = mapped_column(
        String(20), default="internal", comment="密级: public/internal/confidential/secret"
    )
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, comment="上级 ID"
    )
    ldap_dn: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="LDAP DN")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否激活")

    department: Mapped[Department | None] = relationship()


class KbMember(UUIDMixin, TimestampMixin, Base):
    """知识库成员表 — ABAC 数据权限用。"""

    __tablename__ = "kb_members"

    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False, comment="知识库 ID"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="用户 ID"
    )
    role: Mapped[str] = mapped_column(
        String(20), default="viewer", comment="知识库角色: admin/editor/viewer"
    )
