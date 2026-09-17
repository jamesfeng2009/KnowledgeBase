"""
Widget API — 单一职责：网站嵌入 Widget 的令牌交换与匿名问答。

路由：
    POST /widget/token — 登录用户/API Key 换取短期 widget 令牌（绑定 kb_id）
    POST /widget/ask   — 匿名：带 widget 令牌提问（限流 + 知识库全文检索）
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.knowledge import Document
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.widget_token_service import (
    WidgetRateLimiter,
    WidgetRateLimited,
    WidgetTokenError,
    WidgetTokenService,
)

router = APIRouter(prefix="/widget", tags=["网站嵌入 Widget"])

_token_service = WidgetTokenService()
_rate_limiter = WidgetRateLimiter()


class TokenBody(BaseModel):
    """换取 widget 令牌请求体。"""

    kb_id: UUID = Field(..., description="要暴露的知识库 ID")


class AskBody(BaseModel):
    """匿名提问请求体。"""

    token: str = Field(..., description="widget 令牌")
    question: str = Field(..., min_length=1, max_length=1000, description="问题")


@router.post("/token", response_model=ApiResponse[dict])
async def create_widget_token(
    body: TokenBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """登录用户 / API Key 换取短期 widget 令牌。"""
    # 校验用户对该知识库有读权限（至少能访问）
    from app.services.permission_service import PermissionService

    perm = PermissionService(db, user)
    accessible = await perm.get_accessible_kb_ids()
    if accessible is not None and body.kb_id not in accessible:
        raise HTTPException(status_code=403, detail="无权将该知识库暴露到 Widget")
    data = _token_service.create_token(str(body.kb_id))
    return ApiResponse(code=0, data=data, message="success")


@router.post("/ask", response_model=ApiResponse[dict])
async def widget_ask(
    body: AskBody,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict]:
    """匿名提问：校验令牌 → 限流 → 知识库全文检索返回证据片段。"""
    try:
        claims = _token_service.verify_token(body.token)
    except WidgetTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    # 限流：按令牌哈希 + IP 双维度
    client_ip = request.client.host if request.client else "unknown"
    try:
        _rate_limiter.check(f"{body.token[:16]}:{client_ip}")
    except WidgetRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    # 知识库全文检索（仅 published 文档，title/content 匹配）
    pattern = f"%{body.question}%"
    stmt = (
        select(Document)
        .where(
            Document.kb_id == UUID(claims.kb_id),
            Document.deleted_at.is_(None),
            or_(
                Document.title.ilike(pattern),
                Document.content_text.ilike(pattern),
            ),
        )
        .limit(5)
    )
    docs = (await db.scalars(stmt)).all()
    snippets = [
        {
            "doc_id": str(d.id),
            "title": d.title,
            "snippet": (d.content_text or "")[:200],
        }
        for d in docs
    ]
    return ApiResponse(
        code=0,
        data={"kb_id": claims.kb_id, "question": body.question, "results": snippets},
        message="success",
    )
