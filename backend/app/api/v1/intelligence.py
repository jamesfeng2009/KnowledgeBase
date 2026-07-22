"""
文档智能处理 API — 单一职责：提供文档智能处理的 HTTP 端点。

端点：
    POST /intelligence/{doc_id}/process       — 触发文档智能处理
    GET  /intelligence/{doc_id}/status        — 查询处理状态
    PUT  /intelligence/{doc_id}/summary        — 手动修改摘要
    PUT  /intelligence/{doc_id}/tags           — 手动修改标签
    GET  /intelligence/{doc_id}/actions        — 获取行动项列表
    PUT  /intelligence/actions/{action_id}     — 更新行动项状态
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.models.action import DocumentAction
from app.models.knowledge import Document
from app.models.user import User
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


@router.post("/{doc_id}/process")
async def trigger_intelligence(
    request: Request,
    doc_id: str,
    user: User = Depends(require_module("doc_intelligence")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """触发文档智能处理 — 自动摘要/标签/分类/行动项。

    需 admin 或 kb_admin 权限。
    """
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    from uuid import UUID

    from app.llm.factory import get_llm_provider
    from app.services.doc_intelligence_service import DocIntelligenceService

    # 验证文档存在
    result = await db.execute(
        select(Document).where(Document.id == UUID(doc_id))
    )
    doc = result.scalars().first()
    if not doc:
        return ApiResponse(code=404, data=None, message="文档不存在")

    try:
        llm = get_llm_provider()
    except Exception:
        return ApiResponse(code=503, data=None, message="LLM 服务不可用")

    tenant_id = getattr(request.state, "tenant_id", None)
    service = DocIntelligenceService(llm, db, tenant_id=tenant_id)
    result = await service.process_all(doc_id)
    await db.commit()
    return ApiResponse(code=0, data=result, message="success")


@router.get("/{doc_id}/status")
async def get_intelligence_status(
    doc_id: str,
    user: User = Depends(require_module("doc_intelligence")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """查询文档智能处理状态 — 检查摘要/标签/分类是否已生成。"""
    from uuid import UUID

    result = await db.execute(
        select(Document).where(Document.id == UUID(doc_id))
    )
    doc = result.scalars().first()
    if not doc:
        return ApiResponse(code=404, data=None, message="文档不存在")

    status = {
        "has_summary": bool(doc.summary),
        "has_category": bool(doc.category),
        "summary": doc.summary,
        "category": doc.category,
    }
    return ApiResponse(code=0, data=status, message="success")


@router.put("/{doc_id}/summary")
async def update_summary(
    doc_id: str,
    summary: str = Body(..., embed=True),
    user: User = Depends(require_module("doc_intelligence")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """手动修改文档摘要 — 修正 AI 生成结果。"""
    from uuid import UUID

    result = await db.execute(
        select(Document).where(Document.id == UUID(doc_id))
    )
    doc = result.scalars().first()
    if not doc:
        return ApiResponse(code=404, data=None, message="文档不存在")

    doc.summary = summary
    await db.commit()
    return ApiResponse(code=0, data={"summary": summary}, message="success")


@router.put("/{doc_id}/tags")
async def update_tags(
    doc_id: str,
    tags: list[str] = Body(..., embed=True),
    user: User = Depends(require_module("doc_intelligence")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """手动修改文档标签 — 增删 AI 生成的标签。"""
    from uuid import UUID

    result = await db.execute(
        select(Document).where(Document.id == UUID(doc_id))
    )
    doc = result.scalars().first()
    if not doc:
        return ApiResponse(code=404, data=None, message="文档不存在")

    # 更新知识库标签
    if doc.knowledge_base:
        doc.knowledge_base.tags = tags
    await db.commit()
    return ApiResponse(code=0, data={"tags": tags}, message="success")


@router.get("/{doc_id}/actions")
async def get_action_items(
    doc_id: str,
    user: User = Depends(require_module("doc_intelligence")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取文档的行动项列表。"""
    from uuid import UUID

    result = await db.execute(
        select(DocumentAction)
        .where(DocumentAction.doc_id == UUID(doc_id))
        .order_by(DocumentAction.created_at.desc())
    )
    actions = result.scalars().all()
    data = [
        {
            "id": str(a.id),
            "assignee": a.assignee,
            "deadline": str(a.deadline) if a.deadline else None,
            "content": a.content,
            "priority": a.priority,
            "status": a.status,
        }
        for a in actions
    ]
    return ApiResponse(code=0, data=data, message="success")


@router.put("/actions/{action_id}")
async def update_action_item(
    action_id: str,
    status: str = Query(..., description="新状态: pending/completed"),
    user: User = Depends(require_module("doc_intelligence")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """更新行动项状态 — 标记完成/待办。"""
    from uuid import UUID

    result = await db.execute(
        select(DocumentAction).where(DocumentAction.id == UUID(action_id))
    )
    action = result.scalars().first()
    if not action:
        return ApiResponse(code=404, data=None, message="行动项不存在")

    action.status = status
    await db.commit()
    return ApiResponse(code=0, data={"status": status}, message="success")
