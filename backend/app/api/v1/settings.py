"""
系统设置路由 — 单一职责：处理 LLM 配置与系统配置的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
配置持久化委托给配置管理（存储于租户 settings 或独立配置表）。

配置存储策略：
- LLM 配置与系统配置存储在 Tenant.settings JSONB 字段中；
- 私有部署模式下取第一条租户作为默认租户；
- API 密钥字段在响应中掩码显示（如 sk-****1234）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.billing import Tenant
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.settings import (
    LLMConfig,
    LLMConfigUpdate,
    SystemConfig,
    SystemConfigUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["系统设置"])

# 配置在 Tenant.settings 中的键名
_LLM_CONFIG_KEY = "llm_config"
_SYSTEM_CONFIG_KEY = "system_config"

# 默认 LLM 配置
_DEFAULT_LLM_CONFIG: dict[str, Any] = {
    "provider": "vllm",
    "model": "meta-llama/Llama-3.3-70B-Instruct",
    "api_key": None,
    "temperature": 0.7,
    "max_tokens": 2048,
    "base_url": None,
}

# 默认系统配置
_DEFAULT_SYSTEM_CONFIG: dict[str, Any] = {
    "site_name": "Enterprise Knowledge Brain",
    "logo_url": None,
    "default_language": "zh-CN",
    "features": {"chat": True, "search": True, "knowledge": True},
}


def _require_admin(user: User) -> None:
    """校验当前用户是否为管理员。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可执行此操作",
        )


def _mask_api_key(key: str | None) -> str | None:
    """掩码 API 密钥（仅保留前 4 位和后 4 位）。

    短密钥（<12 位，私有部署接内网网关常见）原样返回等于明文泄漏，
    统一返回固定掩码；空值保持原样以便前端区分"未配置"。
    """
    if not key:
        return key
    if len(key) < 12:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


async def _get_tenant_settings(
    db: AsyncSession, user: User | None = None
) -> tuple[Tenant | None, dict[str, Any]]:
    """获取租户及其 settings 字段。

    C6 fix: SaaS 模式按 user.tenant_id 过滤，避免跨租户配置泄漏；
    私有部署 user 为 None 或 tenant_id 为 None 时取第一条（兼容单租户）。

    Args:
        db: 异步数据库会话。
        user: 当前用户（可选，私有部署可为 None）。

    Returns:
        元组 (Tenant 实例, settings dict)。

    Raises:
        HTTPException(404): 找不到租户。
    """
    stmt = select(Tenant).where(Tenant.deleted_at.is_(None))
    # C6 fix: SaaS 模式按用户 tenant_id 过滤
    if user is not None and user.tenant_id is not None:
        stmt = stmt.where(Tenant.id == user.tenant_id)
    stmt = stmt.limit(1)
    result = await db.execute(stmt)
    tenant = result.scalars().first()
    if tenant is None:
        # 私有部署可能没有租户记录，使用内存中的默认配置
        return None, {}  # type: ignore[return-value]
    return tenant, tenant.settings or {}


# ======================================================================
# LLM 配置
# ======================================================================


@router.get("/settings/llm", response_model=ApiResponse[LLMConfig])
async def get_llm_config(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[LLMConfig]:
    """获取 LLM 配置（API 密钥掩码显示）。"""
    tenant, settings = await _get_tenant_settings(db, user)
    llm_raw = settings.get(_LLM_CONFIG_KEY, _DEFAULT_LLM_CONFIG)

    return ApiResponse(
        code=0,
        data=LLMConfig(
            provider=llm_raw.get("provider", _DEFAULT_LLM_CONFIG["provider"]),
            model=llm_raw.get("model", _DEFAULT_LLM_CONFIG["model"]),
            api_key=_mask_api_key(llm_raw.get("api_key")),
            temperature=llm_raw.get("temperature", _DEFAULT_LLM_CONFIG["temperature"]),
            max_tokens=llm_raw.get("max_tokens", _DEFAULT_LLM_CONFIG["max_tokens"]),
            base_url=llm_raw.get("base_url"),
        ),
        message="success",
    )


@router.put("/settings/llm", response_model=ApiResponse[LLMConfig])
async def update_llm_config(
    body: LLMConfigUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[LLMConfig]:
    """更新 LLM 配置（仅 admin 权限）。

    api_key 字段：传入新值时更新，传入 None 或掩码值时保持不变。
    """
    _require_admin(user)

    tenant, settings = await _get_tenant_settings(db, user)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到租户，无法保存配置",
        )

    # 函数式更新：构造新 dict 对象 — settings 是裸 JSONB 列，
    # 原地突变同一对象再赋值回去会被 SQLAlchemy 跳过变更检测，
    # 导致 flush 时 UPDATE 静默丢失。
    llm_raw = dict(settings.get(_LLM_CONFIG_KEY, _DEFAULT_LLM_CONFIG))
    update_fields = body.model_dump(exclude_unset=True)

    for key, value in update_fields.items():
        # api_key 掩码值或空值不覆盖
        if key == "api_key":
            if value is None or (isinstance(value, str) and "****" in value):
                continue
        llm_raw[key] = value

    tenant.settings = {**settings, _LLM_CONFIG_KEY: llm_raw}
    await db.flush()

    return ApiResponse(
        code=0,
        data=LLMConfig(
            provider=llm_raw.get("provider", _DEFAULT_LLM_CONFIG["provider"]),
            model=llm_raw.get("model", _DEFAULT_LLM_CONFIG["model"]),
            api_key=_mask_api_key(llm_raw.get("api_key")),
            temperature=llm_raw.get("temperature", _DEFAULT_LLM_CONFIG["temperature"]),
            max_tokens=llm_raw.get("max_tokens", _DEFAULT_LLM_CONFIG["max_tokens"]),
            base_url=llm_raw.get("base_url"),
        ),
        message="success",
    )


# ======================================================================
# 系统配置
# ======================================================================


@router.get("/settings/system", response_model=ApiResponse[SystemConfig])
async def get_system_config(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[SystemConfig]:
    """获取系统配置。"""
    tenant, settings = await _get_tenant_settings(db, user)
    sys_raw = settings.get(_SYSTEM_CONFIG_KEY, _DEFAULT_SYSTEM_CONFIG)

    return ApiResponse(
        code=0,
        data=SystemConfig(
            site_name=sys_raw.get("site_name", _DEFAULT_SYSTEM_CONFIG["site_name"]),
            logo_url=sys_raw.get("logo_url"),
            default_language=sys_raw.get(
                "default_language", _DEFAULT_SYSTEM_CONFIG["default_language"]
            ),
            features=sys_raw.get("features"),
        ),
        message="success",
    )


@router.put("/settings/system", response_model=ApiResponse[SystemConfig])
async def update_system_config(
    body: SystemConfigUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[SystemConfig]:
    """更新系统配置（仅 admin 权限）。"""
    _require_admin(user)

    tenant, settings = await _get_tenant_settings(db, user)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到租户，无法保存配置",
        )

    # 函数式更新：构造新 dict 对象，原因同上（JSONB 变更检测）。
    sys_raw = dict(settings.get(_SYSTEM_CONFIG_KEY, _DEFAULT_SYSTEM_CONFIG))
    update_fields = body.model_dump(exclude_unset=True)
    sys_raw.update(update_fields)

    tenant.settings = {**settings, _SYSTEM_CONFIG_KEY: sys_raw}
    await db.flush()

    return ApiResponse(
        code=0,
        data=SystemConfig(
            site_name=sys_raw.get("site_name", _DEFAULT_SYSTEM_CONFIG["site_name"]),
            logo_url=sys_raw.get("logo_url"),
            default_language=sys_raw.get(
                "default_language", _DEFAULT_SYSTEM_CONFIG["default_language"]
            ),
            features=sys_raw.get("features"),
        ),
        message="success",
    )
