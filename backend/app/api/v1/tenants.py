"""
租户管理路由 — 单一职责：处理租户配置与用量统计的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
租户数据访问委托给 TenantRepository。

租户信息来源：
- 当前租户通过用户关联或全局单租户（私有部署）确定；
- 私有部署模式下 tenant_id 可为空，使用占位租户信息。

模块门控端点（3.19）：
    GET   /tenants/modules               — 查询所有模块及启用状态
    PUT   /tenants/modules               — 批量更新启用模块（admin）
    PATCH /tenants/modules/{module_id}    — 开关单个模块（admin）
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.billing import Tenant, UsageRecord
from app.models.knowledge import Document, KnowledgeBase
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.settings import TenantConfig, TenantConfigUpdate, TenantUsage
from app.services.tenant_service import TenantService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["租户管理"])


async def _get_current_tenant(
    db: AsyncSession, user: User
) -> Tenant:
    """获取当前用户所属租户。

    私有部署模式下通常只有单个租户，取第一条；
    SaaS 模式下通过用户关联确定（此处简化为取第一条活跃租户）。

    Args:
        db: 异步数据库会话。
        user: 当前用户。

    Returns:
        Tenant 实例。

    Raises:
        HTTPException(404): 找不到租户。
    """
    stmt = select(Tenant).where(Tenant.deleted_at.is_(None)).limit(1)
    result = await db.execute(stmt)
    tenant = result.scalars().first()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到租户信息",
        )
    return tenant


@router.get("/tenants/current", response_model=ApiResponse[TenantConfig])
async def get_current_tenant(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TenantConfig]:
    """获取当前租户信息。"""
    tenant = await _get_current_tenant(db, user)
    return ApiResponse(
        code=0,
        data=TenantConfig(
            id=tenant.id,
            name=tenant.name,
            domain=tenant.domain,
            plan=tenant.plan,
            max_users=tenant.max_users,
            max_storage=tenant.max_storage,
            settings=tenant.settings,
            expired_at=tenant.expired_at,
            created_at=tenant.created_at,
        ),
        message="success",
    )


@router.put("/tenants/current", response_model=ApiResponse[TenantConfig])
async def update_current_tenant(
    body: TenantConfigUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TenantConfig]:
    """更新租户配置（仅 admin 权限）。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可更新租户配置",
        )

    tenant = await _get_current_tenant(db, user)
    update_fields = body.model_dump(exclude_unset=True)
    for key, value in update_fields.items():
        setattr(tenant, key, value)
    await db.flush()
    await db.refresh(tenant)

    return ApiResponse(
        code=0,
        data=TenantConfig(
            id=tenant.id,
            name=tenant.name,
            domain=tenant.domain,
            plan=tenant.plan,
            max_users=tenant.max_users,
            max_storage=tenant.max_storage,
            settings=tenant.settings,
            expired_at=tenant.expired_at,
            created_at=tenant.created_at,
        ),
        message="success",
    )


@router.get("/tenants/usage", response_model=ApiResponse[TenantUsage])
async def get_tenant_usage(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TenantUsage]:
    """获取用量统计（当前用户数、已用存储等）。"""
    tenant = await _get_current_tenant(db, user)

    # 当前用户数
    user_count_stmt = select(func.count(User.id)).where(
        User.deleted_at.is_(None),
        User.is_active.is_(True),
    )
    current_users = await db.scalar(user_count_stmt) or 0

    # 已用存储：统计所有文档 content_text 字段长度之和（近似估算）
    storage_stmt = select(
        func.coalesce(func.sum(func.length(Document.content_text)), 0)
    ).where(Document.deleted_at.is_(None))
    used_storage = await db.scalar(storage_stmt) or 0

    # 文件存储（如有 file_path 则计入）
    file_count_stmt = select(func.count(Document.id)).where(
        Document.deleted_at.is_(None),
        Document.file_path.is_not(None),
    )
    file_count = await db.scalar(file_count_stmt) or 0
    # 每个文件预估平均 500KB
    used_storage += int(file_count) * 500 * 1024

    return ApiResponse(
        code=0,
        data=TenantUsage(
            max_users=tenant.max_users,
            current_users=int(current_users),
            max_storage=tenant.max_storage,
            used_storage=int(used_storage),
            plan=tenant.plan,
            expired_at=tenant.expired_at,
        ),
        message="success",
    )


# ------------------------------------------------------------------
# 模块门控管理（3.19）
# ------------------------------------------------------------------


@router.get("/tenants/modules", response_model=ApiResponse)
async def get_tenant_modules(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """查询当前租户的所有模块及启用状态。

    返回所有已注册模块的列表，每项包含：
    - id: 模块标识
    - name: 中文名称
    - description: 功能描述
    - category: 分类（basic/intelligence/integration）
    - is_basic: 是否基础模块（不可关闭）
    - enabled: 当前租户是否启用
    """
    service = TenantService(db)
    modules = await service.list_modules_with_status()
    return ApiResponse(code=0, data=modules, message="success")


@router.put("/tenants/modules", response_model=ApiResponse)
async def update_tenant_modules(
    module_ids: list[str] = Body(
        ..., embed=True, description="要启用的模块 ID 列表"
    ),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """批量更新租户启用的模块列表（仅 admin）。

    基础模块（knowledge_base / audit_workflow / qa_community）永远包含，
    即使用户未传入也会自动补齐。传入未注册的模块 ID 会返回 400。
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可修改模块配置",
        )

    service = TenantService(db)
    try:
        enabled = await service.update_enabled_modules(module_ids)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await db.commit()
    return ApiResponse(code=0, data={"enabled_modules": enabled}, message="success")


@router.patch("/tenants/modules/{module_id}", response_model=ApiResponse)
async def toggle_tenant_module(
    module_id: str,
    enabled: bool = Body(..., embed=True, description="True 启用 / False 禁用"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """开关单个模块（仅 admin）。

    基础模块不可关闭（静默忽略 enabled=False）。
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可修改模块配置",
        )

    service = TenantService(db)
    try:
        result = await service.toggle_module(module_id, enabled)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await db.commit()
    return ApiResponse(
        code=0,
        data={"module_id": module_id, "enabled_modules": result},
        message="success",
    )
