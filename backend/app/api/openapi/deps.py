"""开放接口认证依赖 — API Key 认证。

与内部 API 的 JWT 认证不同，开放接口使用 API Key：
1. 从 X-API-Key header 获取密钥
2. 在数据库中验证密钥（SHA-256 哈希比对）
3. 检查密钥权限范围（scopes）
4. 检查密钥是否过期/停用
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.services.apikey_service import ApiKeyService
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def get_api_key_user(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """验证 API Key 并返回密钥信息。

    Args:
        x_api_key: X-API-Key header 中的 API 密钥。
        db: 数据库会话。

    Returns:
        密钥信息字典：{key_id, name, scopes, tenant_id}

    Raises:
        HTTPException 401: API Key 无效或已过期。
    """
    service = ApiKeyService(db)
    api_key = await service.validate_key(x_api_key)
    if api_key is None:
        logger.warning("openapi.api_key_invalid")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
        )
    return {
        "key_id": api_key.id,
        "name": api_key.name,
        "scopes": api_key.scopes or [],
        "tenant_id": api_key.tenant_id,
    }


def require_scope(scope: str):
    """依赖工厂：检查 API Key 是否有指定权限范围。

    Args:
        scope: 需要的权限范围（如 "knowledge:read", "llm:chat"）。

    Returns:
        依赖函数。
    """

    async def check_scope(
        api_key_info: dict[str, Any] = Depends(get_api_key_user),
    ) -> dict[str, Any]:
        if scope not in api_key_info.get("scopes", []):
            logger.warning(
                "openapi.scope_denied",
                required_scope=scope,
                key_name=api_key_info.get("name"),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {scope}",
            )
        return api_key_info

    return check_scope
