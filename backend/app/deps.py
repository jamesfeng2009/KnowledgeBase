"""
依赖注入 — 单一职责：提供 FastAPI 依赖注入的公共组件。

遵循单一职责：本模块仅声明认证相关的依赖函数与 OAuth2 scheme，
不包含业务逻辑（业务逻辑由 AuthService 处理）。

遵循依赖倒置：路由通过 ``Depends(get_current_active_user)`` 获取当前用户，
不直接解析 JWT 或查询数据库。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.models.user import User
from app.services.auth_service import AuthService

# OAuth2 Bearer scheme — tokenUrl 指向登录端点，供 Swagger UI 使用。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    """从 JWT 令牌解析当前登录用户。

    将 token 解析委托给 ``AuthService.get_current_user``，
    该方法会校验签名、过期时间，并查询用户是否存在且已激活。

    Args:
        token: JWT access token，由 ``oauth2_scheme`` 从
            ``Authorization: Bearer <token>`` 头中提取。
        db: 异步数据库会话，由 ``get_db_session`` 注入。

    Returns:
        当前已认证的 ``User`` 对象。

    Raises:
        HTTPException(401): 令牌无效 / 用户不存在 / 账号禁用。
    """
    try:
        return await AuthService(db).get_current_user(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_active_user(
    user: User = Depends(get_current_user),
) -> User:
    """获取当前已激活用户 — 在 get_current_user 基础上额外校验 is_active。

    所有需要认证的路由统一使用 ``Depends(get_current_active_user)``，
    确保被禁用的账号无法访问受保护的资源。

    Args:
        user: 由 ``get_current_user`` 注入的当前用户。

    Returns:
        当前已激活的 ``User`` 对象。

    Raises:
        HTTPException(403): 账号已被禁用。
    """
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )
    return user


# ------------------------------------------------------------------
# 租户模块门控 — require_module 工厂函数
# ------------------------------------------------------------------


def require_module(module_name: str):
    """工厂函数：返回一个检查租户模块权限的 FastAPI 依赖。

    在需要按租户套餐门控的 API 端点上使用，替代 ``Depends(get_current_user)``：

    .. code-block:: python

        @router.get("/dashboard")
        async def get_dashboard(
            user: User = Depends(require_module("analytics_dashboard")),
            db: AsyncSession = Depends(get_db_session),
        ) -> ApiResponse:
            ...

    模块名称对应 ``app.core.modules.MODULE_REGISTRY`` 中定义的模块 ID。
    基础模块（is_basic=True）永远通过，不触发数据库查询。

    Args:
        module_name: 模块 ID（如 "doc_intelligence"）。

    Returns:
        FastAPI 依赖函数，返回当前已认证用户。
        若租户未启用该模块，抛出 HTTP 403。
    """

    async def _check_module(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db_session),
    ) -> User:
        # 延迟导入避免循环依赖
        from app.services.tenant_service import TenantService

        service = TenantService(db)
        if not await service.is_module_enabled(module_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"当前套餐未包含「{module_name}」功能",
            )
        return user

    return _check_module
