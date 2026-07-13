"""
认证相关 Schema — 单一职责：用户注册、登录、令牌与用户响应的数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，不包含密码哈希、令牌签发等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# 邮箱正则：兼容 ASCII 本地域与域名，不依赖 email-validator 第三方包
_EMAIL_PATTERN = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"


class UserRole(str, Enum):
    """用户角色。"""

    admin = "admin"
    kb_admin = "kb_admin"
    editor = "editor"
    viewer = "viewer"


class ClearanceLevel(str, Enum):
    """数据密级。"""

    public = "public"
    internal = "internal"
    confidential = "confidential"
    secret = "secret"


class UserCreate(BaseModel):
    """用户注册请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    email: str = Field(
        ..., pattern=_EMAIL_PATTERN, max_length=255, description="邮箱"
    )
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="密码（明文，传输后由服务端哈希存储）",
    )
    name: str = Field(..., min_length=1, max_length=100, description="姓名")


class UserLogin(BaseModel):
    """用户登录请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    email: str = Field(..., pattern=_EMAIL_PATTERN, max_length=255, description="邮箱")
    password: str = Field(..., min_length=1, max_length=128, description="密码")


class UserResponse(BaseModel):
    """用户响应 — 不含密码哈希。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="用户 ID")
    email: str = Field(..., description="邮箱")
    name: str = Field(..., description="姓名")
    avatar: str | None = Field(default=None, description="头像 URL")
    role: UserRole = Field(default=UserRole.viewer, description="角色")
    dept_id: uuid.UUID | None = Field(default=None, description="部门 ID")
    clearance_level: ClearanceLevel = Field(
        default=ClearanceLevel.internal, description="数据密级"
    )
    is_active: bool = Field(default=True, description="是否激活")
    created_at: datetime = Field(..., description="创建时间")


class TokenResponse(BaseModel):
    """登录令牌响应。"""

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(..., description="JWT 访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    user: UserResponse = Field(..., description="当前登录用户信息")
