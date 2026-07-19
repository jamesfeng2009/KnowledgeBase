"""
系统设置 Schema — 单一职责：LLM 配置、系统配置、API 密钥与租户配置的数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，
不包含配置持久化、密钥生成等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LLMConfig(BaseModel):
    """LLM 配置 — 模型提供商与参数。"""

    model_config = ConfigDict(from_attributes=True)

    provider: str = Field(
        ..., max_length=50, description="提供商: openai/anthropic/cohere/vllm"
    )
    model: str = Field(..., max_length=100, description="模型名称")
    api_key: str | None = Field(
        default=None,
        description="API 密钥（响应中掩码显示，如 sk-****1234）",
    )
    temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, description="温度参数"
    )
    max_tokens: int = Field(
        default=2048, ge=1, le=32768, description="最大生成 token 数"
    )
    base_url: str | None = Field(
        default=None, max_length=500, description="自定义 API 地址（私有部署）"
    )


class LLMConfigUpdate(BaseModel):
    """LLM 配置更新请求 — 所有字段可选。"""

    model_config = ConfigDict(from_attributes=True)

    provider: str | None = Field(default=None, max_length=50)
    model: str | None = Field(default=None, max_length=100)
    api_key: str | None = Field(default=None, description="新的 API 密钥明文")
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    base_url: str | None = Field(default=None, max_length=500)


class SystemConfig(BaseModel):
    """系统配置 — 站点级设置。"""

    model_config = ConfigDict(from_attributes=True)

    site_name: str = Field(default="Enterprise Knowledge Brain", max_length=255, description="站点名称")
    logo_url: str | None = Field(default=None, max_length=500, description="Logo URL")
    default_language: str = Field(default="zh-CN", max_length=10, description="默认语言")
    features: dict[str, Any] | None = Field(
        default=None, description="功能开关（JSONB，如 {chat: true, search: true}）"
    )


class SystemConfigUpdate(BaseModel):
    """系统配置更新请求 — 所有字段可选。"""

    model_config = ConfigDict(from_attributes=True)

    site_name: str | None = Field(default=None, max_length=255)
    logo_url: str | None = Field(default=None, max_length=500)
    default_language: str | None = Field(default=None, max_length=10)
    features: dict[str, Any] | None = None


class ApiKeyCreate(BaseModel):
    """API 密钥创建请求。"""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., min_length=1, max_length=255, description="密钥名称")
    scopes: list[str] | None = Field(
        default=None, description="授权范围列表（如 [read, write]）"
    )
    expires_at: datetime | None = Field(
        default=None, description="过期时间（为空表示永不过期）"
    )


class ApiKeyResponse(BaseModel):
    """API 密钥响应 — 不含完整密钥（仅前缀）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="密钥 ID")
    name: str = Field(..., description="密钥名称")
    key_prefix: str = Field(..., description="密钥前缀（明文前 8 位）")
    scopes: list[str] | None = Field(default=None, description="授权范围")
    expires_at: datetime | None = Field(
        default=None, description="过期时间（为空表示永不过期）"
    )
    tenant_id: uuid.UUID | None = Field(
        default=None, description="租户 ID（多租户隔离）"
    )
    created_at: datetime = Field(..., description="创建时间")
    last_used_at: datetime | None = Field(
        default=None, description="最后使用时间"
    )
    is_active: bool = Field(default=True, description="是否启用")


class ApiKeyCreateResponse(BaseModel):
    """API 密钥创建响应 — 仅创建时返回完整密钥。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="密钥 ID")
    name: str = Field(..., description="密钥名称")
    key: str = Field(..., description="完整 API 密钥（仅此一次显示）")
    key_prefix: str = Field(..., description="密钥前缀")
    scopes: list[str] | None = Field(default=None, description="授权范围")
    expires_at: datetime | None = Field(
        default=None, description="过期时间（为空表示永不过期）"
    )
    tenant_id: uuid.UUID | None = Field(
        default=None, description="租户 ID（多租户隔离）"
    )
    created_at: datetime = Field(..., description="创建时间")


class TenantConfig(BaseModel):
    """租户配置 — 当前租户信息与设置。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="租户 ID")
    name: str = Field(..., description="租户名称")
    domain: str | None = Field(default=None, description="域名")
    plan: str = Field(default="free", description="套餐: free/pro/enterprise")
    max_users: int = Field(default=10, description="最大用户数")
    max_storage: int = Field(default=0, description="最大存储（字节）")
    settings: dict[str, Any] | None = Field(
        default=None, description="租户配置"
    )
    expired_at: datetime | None = Field(default=None, description="到期时间")
    created_at: datetime = Field(..., description="创建时间")


class TenantConfigUpdate(BaseModel):
    """租户配置更新请求 — 所有字段可选。"""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: dict[str, Any] | None = None


class TenantUsage(BaseModel):
    """租户用量统计。"""

    model_config = ConfigDict(from_attributes=True)

    max_users: int = Field(..., description="最大用户数")
    current_users: int = Field(..., description="当前用户数")
    max_storage: int = Field(..., description="最大存储（字节）")
    used_storage: int = Field(default=0, description="已用存储（字节）")
    plan: str = Field(..., description="当前套餐")
    expired_at: datetime | None = Field(default=None, description="到期时间")
