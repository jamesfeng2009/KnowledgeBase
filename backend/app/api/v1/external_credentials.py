"""外部凭证管理 API — P0+P3 外部文档实时同步的凭证管理。

端点：
    GET    /external/adapters                   — 列出支持的适配器
    GET    /external/credentials                — 列出已保存的凭证（不含明文）
    POST   /external/credentials                — 创建/更新凭证（加密存储）
    DELETE /external/credentials/{adapter_id}    — 删除凭证
    POST   /external/credentials/{adapter_id}/test — 测试凭证连通性
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.models.knowledge import ExternalCredential
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.external_credential import (
    AdapterInfo,
    CredentialTestResult,
    ExternalCredentialCreate,
    ExternalCredentialResponse,
)
from app.utils.crypto import encrypt_secret
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/external", tags=["external-credentials"])


def _require_admin(user: User) -> None:
    """校验管理员权限。"""
    if user.role not in ("admin", "kb_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")


@router.get("/adapters")
async def list_adapters(
    user: User = Depends(require_module("external_sync")),
) -> ApiResponse:
    """列出所有支持的适配器。"""
    from app.document.source_adapters.registry import adapter_registry

    adapters = adapter_registry.list_adapters()
    return ApiResponse(
        code=0,
        data=[AdapterInfo(**a) for a in adapters],
        message="success",
    )


@router.get("/credentials")
async def list_credentials(
    user: User = Depends(require_module("external_sync")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """列出已保存的外部凭证（不返回 credentials 明文）。"""
    _require_admin(user)

    result = await db.execute(select(ExternalCredential))
    creds = result.scalars().all()
    return ApiResponse(
        code=0,
        data=[
            ExternalCredentialResponse.model_validate(c) for c in creds
        ],
        message="success",
    )


@router.post("/credentials")
async def create_or_update_credential(
    payload: ExternalCredentialCreate,
    user: User = Depends(require_module("external_sync")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """创建或更新外部凭证（加密存储）。

    同一 (tenant_id, adapter_id) 唯一 — 存在则更新，不存在则创建。
    凭证用 AES-GCM 加密后存储（详见 app.utils.crypto.encrypt_secret）。
    """
    _require_admin(user)

    from app.document.source_adapters.registry import adapter_registry

    # 校验 adapter_id 是否合法
    adapter = adapter_registry.get(payload.adapter_id)
    if adapter is None:
        return ApiResponse(
            code=400,
            data=None,
            message=f"不支持的适配器: {payload.adapter_id}",
        )

    # 当前实现：私有部署 tenant_id 为 None
    # 多租户场景应从 user 上下文获取 tenant_id
    tenant_id = getattr(user, "tenant_id", None)

    # 加密凭证
    plaintext = json.dumps(payload.credentials, ensure_ascii=False)
    encrypted = encrypt_secret(plaintext)

    # 查询是否已存在
    stmt = select(ExternalCredential).where(
        ExternalCredential.adapter_id == payload.adapter_id,
    )
    if tenant_id is not None:
        stmt = stmt.where(ExternalCredential.tenant_id == tenant_id)
    else:
        stmt = stmt.where(ExternalCredential.tenant_id.is_(None))

    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        # 更新
        existing.credentials_encrypted = encrypted
        existing.is_active = payload.is_active
        await db.commit()
        await db.refresh(existing)
        log.info(
            "external_credential.updated",
            adapter_id=payload.adapter_id,
            user_id=str(user.id),
        )
        return ApiResponse(
            code=0,
            data=ExternalCredentialResponse.model_validate(existing),
            message="凭证已更新",
        )

    # 创建
    cred = ExternalCredential(
        tenant_id=tenant_id,
        adapter_id=payload.adapter_id,
        credentials_encrypted=encrypted,
        is_active=payload.is_active,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    log.info(
        "external_credential.created",
        adapter_id=payload.adapter_id,
        user_id=str(user.id),
    )
    return ApiResponse(
        code=0,
        data=ExternalCredentialResponse.model_validate(cred),
        message="凭证已创建",
    )


@router.delete("/credentials/{adapter_id}")
async def delete_credential(
    adapter_id: str,
    user: User = Depends(require_module("external_sync")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """删除外部凭证。"""
    _require_admin(user)

    tenant_id = getattr(user, "tenant_id", None)
    stmt = select(ExternalCredential).where(
        ExternalCredential.adapter_id == adapter_id,
    )
    if tenant_id is not None:
        stmt = stmt.where(ExternalCredential.tenant_id == tenant_id)
    else:
        stmt = stmt.where(ExternalCredential.tenant_id.is_(None))

    result = await db.execute(stmt)
    cred = result.scalar_one_or_none()
    if cred is None:
        return ApiResponse(code=404, data=None, message="凭证不存在")

    await db.delete(cred)
    await db.commit()
    log.info(
        "external_credential.deleted",
        adapter_id=adapter_id,
        user_id=str(user.id),
    )
    return ApiResponse(code=0, data=None, message="凭证已删除")


@router.post("/credentials/{adapter_id}/test")
async def test_credential(
    adapter_id: str,
    user: User = Depends(require_module("external_sync")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """测试指定适配器的凭证连通性。"""
    _require_admin(user)

    from app.document.source_adapters.registry import adapter_registry
    from app.utils.crypto import decrypt_secret

    adapter = adapter_registry.get(adapter_id)
    if adapter is None:
        return ApiResponse(
            code=404, data=None, message=f"适配器不存在: {adapter_id}"
        )

    # 读取凭证
    tenant_id = getattr(user, "tenant_id", None)
    stmt = select(ExternalCredential).where(
        ExternalCredential.adapter_id == adapter_id,
        ExternalCredential.is_active.is_(True),
    )
    if tenant_id is not None:
        stmt = stmt.where(ExternalCredential.tenant_id == tenant_id)
    else:
        stmt = stmt.where(ExternalCredential.tenant_id.is_(None))

    result = await db.execute(stmt)
    cred = result.scalar_one_or_none()
    if cred is None:
        return ApiResponse(
            code=404,
            data=CredentialTestResult(
                adapter_id=adapter_id,
                connected=False,
                message="未找到启用的凭证",
            ),
            message="未找到启用的凭证",
        )

    # 解密并测试
    try:
        plaintext = decrypt_secret(cred.credentials_encrypted)
        credentials = json.loads(plaintext)
    except Exception as exc:
        log.warning(
            "external_credential.decrypt_failed",
            adapter_id=adapter_id,
            error=str(exc)[:200],
        )
        return ApiResponse(
            code=500,
            data=CredentialTestResult(
                adapter_id=adapter_id,
                connected=False,
                message="凭证解密失败",
            ),
            message="凭证解密失败",
        )

    connected = await adapter.test_connection(credentials)
    return ApiResponse(
        code=0,
        data=CredentialTestResult(
            adapter_id=adapter_id,
            connected=connected,
            message="连接成功" if connected else "连接失败",
        ),
        message="success",
    )
