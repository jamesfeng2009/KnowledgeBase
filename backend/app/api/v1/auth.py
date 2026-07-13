"""
认证路由 — 单一职责：处理用户注册、登录与当前用户信息的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（密码哈希、JWT 签发、用户查询）委托给 AuthService。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.auth import TokenResponse, UserCreate, UserResponse
from app.schemas.common import ApiResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/register", response_model=ApiResponse[UserResponse], status_code=201)
async def register(
    body: UserCreate,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[UserResponse]:
    """用户注册。

    接收邮箱、密码、姓名，经 AuthService 完成密码哈希与用户创建后，
    返回不含密码哈希的 UserResponse。

    业务异常：
    - 邮箱已被注册 → 409 Conflict
    """
    service = AuthService(db)
    try:
        user = await service.register(body.email, body.password, body.name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return ApiResponse(
        code=0,
        data=UserResponse.model_validate(user),
        message="success",
    )


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[TokenResponse]:
    """用户登录（OAuth2 兼容）。

    使用 OAuth2PasswordRequestForm 接收表单数据（username 即邮箱），
    经 AuthService 校验密码后签发 JWT 令牌。

    业务异常：
    - 邮箱不存在 / 密码错误 / 账号禁用 → 401 Unauthorized
    """
    service = AuthService(db)
    try:
        token = await service.login(form_data.username, form_data.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return ApiResponse(code=0, data=token, message="success")


@router.get("/me", response_model=ApiResponse[UserResponse])
async def get_me(
    user: User = Depends(get_current_active_user),
) -> ApiResponse[UserResponse]:
    """获取当前登录用户信息。"""
    return ApiResponse(
        code=0,
        data=UserResponse.model_validate(user),
        message="success",
    )
