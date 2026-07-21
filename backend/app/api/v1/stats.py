"""
统计概览 API — 单一职责：为首页工作台提供四项核心统计指标。

端点：
    GET /stats/overview — 文档总数 / 知识库数 / 对话次数 / 活跃用户数
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.conversation import Conversation
from app.models.knowledge import Document, KnowledgeBase
from app.models.user import User
from app.schemas.common import ApiResponse

router = APIRouter(tags=["统计"])


@router.get("/stats/overview")
async def get_stats_overview(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取工作台四项核心统计。

    所有登录用户均可查看，数据不做租户/权限隔离（展示全局规模）。
    """
    # 文档总数（未软删除）
    doc_count = await db.scalar(
        select(func.count()).select_from(Document).where(Document.deleted_at.is_(None))
    )

    # 知识库数（未软删除）
    kb_count = await db.scalar(
        select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None))
    )

    # 对话次数（未软删除）
    conv_count = await db.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.deleted_at.is_(None))
    )

    # 活跃用户数：最近 30 天有消息记录的用户数
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    active_users = await db.scalar(
        select(func.count(func.distinct(Conversation.user_id)))
        .where(Conversation.created_at >= thirty_days_ago)
        .where(Conversation.deleted_at.is_(None))
    )

    return ApiResponse(
        code=0,
        data={
            "total_documents": doc_count or 0,
            "total_knowledge_bases": kb_count or 0,
            "total_conversations": conv_count or 0,
            "active_users": active_users or 0,
        },
        message="success",
    )
