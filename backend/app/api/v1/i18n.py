"""
i18n API — 单一职责：暴露支持语言列表与翻译资源。

路由：
    GET /i18n/locales            — 支持的语言列表（供前端语言切换）
    GET /i18n/{locale}/messages  — 指定语言的完整翻译资源
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.i18n.service import (
    SUPPORTED_LOCALES,
    all_translations,
    get_supported_locales,
)
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/i18n", tags=["i18n 国际化"])


@router.get("/locales", response_model=ApiResponse[list[dict]])
async def locales() -> ApiResponse[list[dict]]:
    """支持的语言列表。"""
    return ApiResponse(code=0, data=get_supported_locales(), message="success")


@router.get("/{locale}/messages", response_model=ApiResponse[dict])
async def messages(locale: str) -> ApiResponse[dict]:
    """指定语言的完整翻译资源（zh/en/ja/ko）。"""
    if locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=404, detail=f"不支持的 locale: {locale}")
    return ApiResponse(
        code=0,
        data=all_translations(locale),
        message="success",
    )
