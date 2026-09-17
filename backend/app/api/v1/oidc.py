"""
OIDC 认证路由 — 单一职责：OIDC 登录跳转与回调。

路由：
    GET /auth/oidc/login    — 302 跳转 OIDC 授权端点
    GET /auth/oidc/callback — code 交换 → userinfo → 登录/注册 → 返回 JWT
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.schemas.auth import TokenResponse
from app.schemas.common import ApiResponse
from app.services.oidc_service import OIDCDisabledError, OIDCError, OIDCService

router = APIRouter(prefix="/auth/oidc", tags=["认证 · OIDC"])


@router.get("/login")
async def oidc_login(
    db: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """OIDC 登录：跳转授权端点。"""
    service = OIDCService(db)
    try:
        url, state = service.build_authorize_url()
    except OIDCDisabledError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OIDCError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # 生产环境应将 state 落 cookie / Redis 供回调校验（CSRF）
    return RedirectResponse(url=url, status_code=302)


@router.get("/callback", response_model=ApiResponse[TokenResponse])
async def oidc_callback(
    code: str = Query(...),
    state: str = Query(default=""),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[TokenResponse]:
    """OIDC 回调：令牌交换 → userinfo → 登录/注册 → 本系统 JWT。"""
    del state
    service = OIDCService(db)
    try:
        tokens = await service.exchange_code(code)
    except (OIDCDisabledError, OIDCError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    access_token = tokens.get("access_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail="OIDC 未返回 access_token")

    try:
        userinfo = await service.get_userinfo(access_token)
        result = await service.login_or_register(userinfo)
    except OIDCError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ApiResponse(
        code=0,
        data=TokenResponse(
            access_token=result["access_token"],
            token_type="bearer",
        ),
        message="success",
    )
