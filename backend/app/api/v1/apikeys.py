"""
API 密钥管理路由 — 单一职责：处理 API 密钥的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
密钥生成、哈希、校验等安全逻辑委托给 ApiKeyService。

安全说明：
- 创建密钥时返回完整明文（仅此一次），后续列表只显示前缀；
- 停用密钥为软停用（is_active = False），不物理删除。
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.settings import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyResponse,
)
from app.services.apikey_service import ApiKeyService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["API 密钥管理"])


@router.get("/apikeys", response_model=ApiResponse[list[ApiKeyResponse]])
async def list_api_keys(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[ApiKeyResponse]]:
    """获取 API 密钥列表（不含明文密钥）。"""
    service = ApiKeyService(db)
    keys = await service.list_keys()

    return ApiResponse(
        code=0,
        data=[
            ApiKeyResponse(
                id=k.id,
                name=k.name,
                key_prefix=k.key_prefix,
                scopes=k.scopes,
                created_at=k.created_at,
                last_used_at=k.last_used_at,
                is_active=k.is_active,
            )
            for k in keys
        ],
        message="success",
    )


@router.post(
    "/apikeys",
    response_model=ApiResponse[ApiKeyCreateResponse],
    status_code=201,
)
async def create_api_key(
    body: ApiKeyCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[ApiKeyCreateResponse]:
    """创建 API 密钥，返回完整密钥（仅此一次显示）。

    创建后请妥善保存密钥，后续无法再次获取明文。
    """
    service = ApiKeyService(db)
    api_key, plaintext = await service.create_key(
        name=body.name,
        scopes=body.scopes,
        expires_at=body.expires_at,
    )

    return ApiResponse(
        code=0,
        data=ApiKeyCreateResponse(
            id=api_key.id,
            name=api_key.name,
            key=plaintext,
            key_prefix=api_key.key_prefix,
            scopes=api_key.scopes,
            created_at=api_key.created_at,
        ),
        message="success",
    )


@router.delete("/apikeys/{key_id}", response_model=ApiResponse)
async def revoke_api_key(
    key_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """停用 API 密钥（软停用，不物理删除）。"""
    service = ApiKeyService(db)
    result = await service.revoke_key(key_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API 密钥 {key_id} 不存在",
        )

    return ApiResponse(code=0, message="success")
