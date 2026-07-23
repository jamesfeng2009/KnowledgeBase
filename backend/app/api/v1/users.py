"""
用户权限管理路由 — 单一职责：处理用户与部门管理的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
用户数据访问委托给 UserRepository，LDAP 同步委托给 LDAPSyncService。

权限策略：
- 用户列表与详情：所有已认证用户可查看；
- 修改角色：仅 admin 可操作；
- LDAP 同步：仅 admin 可触发。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import Department, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import UserResponse, UserRole
from app.schemas.common import ApiResponse, PageResponse
from app.services.ldap_sync_service import LDAPSyncService
from app.utils.pagination import PageResult, PaginationParams, paginate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["用户管理"])


def _require_admin(user: User) -> None:
    """校验当前用户是否为管理员。"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可执行此操作",
        )


# ======================================================================
# 用户 CRUD
# ======================================================================


@router.get("/users", response_model=ApiResponse[PageResponse[UserResponse]])
async def list_users(
    keyword: str | None = Query(default=None, description="姓名/邮箱关键词"),
    role: str | None = Query(default=None, description="按角色过滤"),
    dept_id: UUID | None = Query(default=None, description="按部门过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[UserResponse]]:
    """分页查询用户列表（支持关键词、角色、部门过滤）。

    C6 fix: SaaS 模式按当前用户 tenant_id 过滤，避免跨租户用户信息泄漏。
    """
    params = PaginationParams(page=page, size=size)

    stmt = select(User).where(User.deleted_at.is_(None))
    # C6 fix: 按当前用户 tenant_id 过滤
    if user.tenant_id is not None:
        stmt = stmt.where(User.tenant_id == user.tenant_id)
    if keyword:
        pattern = f"%{keyword}%"
        stmt = stmt.where(
            (User.name.ilike(pattern)) | (User.email.ilike(pattern))
        )
    if role:
        stmt = stmt.where(User.role == role)
    if dept_id:
        stmt = stmt.where(User.dept_id == dept_id)

    stmt = stmt.order_by(User.created_at.desc())
    result: PageResult = await paginate(stmt, params, db)

    return ApiResponse(
        code=0,
        data=PageResponse[UserResponse](
            items=[UserResponse.model_validate(u) for u in result.items],
            total=result.total,
            page=result.page,
            size=result.size,
            pages=result.pages,
        ),
        message="success",
    )


@router.get("/users/{user_id}", response_model=ApiResponse[UserResponse])
async def get_user(
    request: Request,
    user_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[UserResponse]:
    """获取用户详情。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    user_repo = UserRepository(db, tenant_id=tenant_id)
    target = await user_repo.get_by_id(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )
    return ApiResponse(
        code=0,
        data=UserResponse.model_validate(target),
        message="success",
    )


@router.put("/users/{user_id}/role", response_model=ApiResponse[UserResponse])
async def update_user_role(
    request: Request,
    user_id: UUID,
    role: UserRole = Query(..., description="新角色: admin/kb_admin/editor/viewer"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[UserResponse]:
    """修改用户角色（仅 admin 权限）。"""
    _require_admin(user)

    tenant_id = getattr(request.state, "tenant_id", None)
    user_repo = UserRepository(db, tenant_id=tenant_id)
    target = await user_repo.get_by_id(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )

    # 不允许管理员降级自己
    if target.id == user.id and role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不允许修改自己的角色",
        )

    updated = await user_repo.update(user_id, role=role.value)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )

    return ApiResponse(
        code=0,
        data=UserResponse.model_validate(updated),
        message="success",
    )


# ======================================================================
# 部门管理
# ======================================================================


@router.get("/departments", response_model=ApiResponse[list[dict[str, Any]]])
async def list_departments(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[dict[str, Any]]]:
    """获取部门列表（树形结构）。

    返回嵌套的部门树，每个节点包含 id / name / parent_id / children。
    """
    # 查询所有部门
    stmt = (
        select(Department)
        .where(Department.deleted_at.is_(None))
        .order_by(Department.sort_order, Department.name)
    )
    result = await db.execute(stmt)
    departments = list(result.scalars().all())

    # 构建部门树
    dept_map: dict[UUID, dict[str, Any]] = {}
    for dept in departments:
        dept_map[dept.id] = {
            "id": str(dept.id),
            "name": dept.name,
            "parent_id": str(dept.parent_id) if dept.parent_id else None,
            "sort_order": dept.sort_order,
            "children": [],
        }

    roots: list[dict[str, Any]] = []
    for dept in departments:
        node = dept_map[dept.id]
        if dept.parent_id and dept.parent_id in dept_map:
            dept_map[dept.parent_id]["children"].append(node)
        else:
            roots.append(node)

    return ApiResponse(
        code=0,
        data=roots,
        message="success",
    )


# ======================================================================
# LDAP 同步
# ======================================================================


@router.post("/users/sync-ldap", response_model=ApiResponse[dict[str, Any]])
async def sync_ldap(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict[str, Any]]:
    """触发 LDAP 同步（仅 admin 权限）。

    同步用户与组织架构到本地数据库。
    当 LDAP 未配置时，返回空结果不报错。
    """
    _require_admin(user)

    ldap_service = LDAPSyncService(db)
    users = await ldap_service.sync_users()
    depts = await ldap_service.sync_org_tree()

    return ApiResponse(
        code=0,
        data={
            "synced_users": len(users),
            "synced_departments": len(depts),
            "users": users,
            "departments": depts,
        },
        message="success",
    )
