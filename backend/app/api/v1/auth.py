"""
认证路由 — 单一职责：处理用户注册、登录与当前用户信息的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（密码哈希、JWT 签发、用户查询）委托给 AuthService。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
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

    C6 fix: SaaS 模式默认关闭开放注册（REGISTRATION_ENABLED=False），
    私有部署可通过环境变量 REGISTRATION_ENABLED=true 开启。

    业务异常：
    - 注册已关闭 → 403 Forbidden
    - 邮箱已被注册 → 409 Conflict
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.REGISTRATION_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="注册功能未开放，请联系管理员创建账号",
        )

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
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[TokenResponse]:
    """用户登录（OAuth2 兼容）。

    使用 OAuth2PasswordRequestForm 接收表单数据（username 即邮箱），
    经 AuthService 校验密码后签发 JWT 令牌。

    P0 安全修复：登录成功后通过 ``Set-Cookie`` 返回 HttpOnly Cookie，
    前端浏览器请求自动携带，避免 JWT 落入 localStorage 被 XSS 读取。
    响应体中仍保留 access_token，供移动应用 / 第三方客户端使用。

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

    settings = get_settings()
    max_age = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    response.set_cookie(
        key="access_token",
        value=token.access_token,
        httponly=True,
        secure=not settings.DEBUG,  # 生产环境强制 HTTPS
        samesite="lax",
        max_age=max_age,
    )

    return ApiResponse(code=0, data=token, message="success")


@router.post("/logout", response_model=ApiResponse)
async def logout(
    response: Response,
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """用户登出 — 清除 HttpOnly Cookie。

    P0 安全修复：服务端清除 cookie，确保浏览器端 token 失效，
    不再依赖前端 localStorage.removeItem。
    """
    response.delete_cookie(key="access_token")
    return ApiResponse(code=0, message="success")


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
