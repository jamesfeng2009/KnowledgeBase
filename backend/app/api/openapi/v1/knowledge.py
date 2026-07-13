"""知识库开放 API — 单一职责：面向外部系统的只读知识库查询。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
数据查询通过 SQLAlchemy 直接访问（按 API Key 的 tenant_id 隔离），
不依赖内部 v1 的 JWT 用户上下文。

权限说明：
- 认证方式为 API Key（X-API-Key header），非 JWT；
- 所有查询受限于 API Key 的 tenant_id（多租户隔离）；
- 仅暴露只读接口，不提供创建/修改/删除能力；
- 需要 scope: ``knowledge:read``。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi.deps import require_scope
from app.database import get_db_session
from app.models.knowledge import Document, KnowledgeBase
from app.schemas.common import ApiResponse, PageResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/knowledge", tags=["开放接口-知识库"])


def _kb_to_dict(kb: KnowledgeBase) -> dict:
    """将 KnowledgeBase ORM 实例序列化为开放 API 响应字典。"""
    return {
        "id": str(kb.id),
        "name": kb.name,
        "description": kb.description,
        "visibility": kb.visibility,
        "owner_id": str(kb.owner_id),
        "tags": kb.tags or [],
        "created_at": kb.created_at.isoformat() if kb.created_at else None,
        "updated_at": kb.updated_at.isoformat() if kb.updated_at else None,
    }


def _doc_to_dict(doc: Document) -> dict:
    """将 Document ORM 实例序列化为开放 API 响应字典。"""
    return {
        "id": str(doc.id),
        "kb_id": str(doc.kb_id),
        "title": doc.title,
        "doc_type": doc.doc_type,
        "status": doc.status,
        "classification": doc.classification,
        "owner_id": str(doc.owner_id),
        "content_preview": (doc.content_text or "")[:500],
        "view_count": doc.view_count,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


@router.get("/bases", response_model=ApiResponse[PageResponse[dict]])
async def list_knowledge_bases(
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    api_key_info: dict = Depends(require_scope("knowledge:read")),
) -> ApiResponse[PageResponse[dict]]:
    """分页查询知识库列表（按 API Key 的 tenant_id 隔离）。

    仅返回未软删除的知识库，按创建时间倒序排列。
    """
    tenant_id = api_key_info.get("tenant_id")

    base_stmt = select(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None))
    if tenant_id is not None:
        base_stmt = base_stmt.where(KnowledgeBase.tenant_id == tenant_id)

    # 总数
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = await db.scalar(count_stmt)
    total = int(total) if total is not None else 0

    # 分页数据
    stmt = (
        base_stmt.order_by(KnowledgeBase.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(stmt)
    items = [_kb_to_dict(kb) for kb in result.scalars().all()]

    pages = (total + size - 1) // size if size > 0 else 0

    return ApiResponse(
        code=0,
        data=PageResponse[dict](
            items=items,
            total=total,
            page=page,
            size=size,
            pages=pages,
        ),
        message="success",
    )


@router.get(
    "/bases/{kb_id}/documents",
    response_model=ApiResponse[PageResponse[dict]],
)
async def list_documents(
    kb_id: UUID,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    api_key_info: dict = Depends(require_scope("knowledge:read")),
) -> ApiResponse[PageResponse[dict]]:
    """分页查询指定知识库下的文档列表。

    仅返回未软删除的文档，按创建时间倒序排列。
    """
    tenant_id = api_key_info.get("tenant_id")

    base_stmt = select(Document).where(
        Document.kb_id == kb_id,
        Document.deleted_at.is_(None),
    )
    if tenant_id is not None:
        base_stmt = base_stmt.where(Document.tenant_id == tenant_id)

    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = await db.scalar(count_stmt)
    total = int(total) if total is not None else 0

    stmt = (
        base_stmt.order_by(Document.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(stmt)
    items = [_doc_to_dict(doc) for doc in result.scalars().all()]

    pages = (total + size - 1) // size if size > 0 else 0

    return ApiResponse(
        code=0,
        data=PageResponse[dict](
            items=items,
            total=total,
            page=page,
            size=size,
            pages=pages,
        ),
        message="success",
    )


@router.get("/documents/{doc_id}", response_model=ApiResponse[dict])
async def get_document(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    api_key_info: dict = Depends(require_scope("knowledge:read")),
) -> ApiResponse[dict]:
    """获取文档详情（含完整纯文本内容）。

    Raises:
        HTTPException 404: 文档不存在或已删除。
    """
    tenant_id = api_key_info.get("tenant_id")

    stmt = select(Document).where(
        Document.id == doc_id,
        Document.deleted_at.is_(None),
    )
    if tenant_id is not None:
        stmt = stmt.where(Document.tenant_id == tenant_id)

    result = await db.execute(stmt)
    doc = result.scalars().first()
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {doc_id} 不存在或无权访问",
        )

    data = _doc_to_dict(doc)
    data["content_text"] = doc.content_text
    return ApiResponse(code=0, data=data, message="success")


@router.get("/search", response_model=ApiResponse[dict])
async def search(
    q: str = Query(..., min_length=1, max_length=1000, description="查询词"),
    kb_id: UUID | None = Query(default=None, description="限定知识库 ID"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    api_key_info: dict = Depends(require_scope("knowledge:read")),
) -> ApiResponse[dict]:
    """全文搜索知识库文档（基于 PostgreSQL ILIKE）。

    在文档标题与纯文本内容中匹配查询词，返回带高亮摘要的结果。
    """
    tenant_id = api_key_info.get("tenant_id")
    pattern = f"%{q}%"

    base_stmt = select(Document).where(
        Document.deleted_at.is_(None),
        Document.content_text.ilike(pattern) | Document.title.ilike(pattern),
    )
    if kb_id is not None:
        base_stmt = base_stmt.where(Document.kb_id == kb_id)
    if tenant_id is not None:
        base_stmt = base_stmt.where(Document.tenant_id == tenant_id)

    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = await db.scalar(count_stmt)
    total = int(total) if total is not None else 0

    stmt = (
        base_stmt.order_by(Document.view_count.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(stmt)
    docs = result.scalars().all()

    items = []
    for doc in docs:
        text = doc.content_text or ""
        idx = text.lower().find(q.lower())
        start = max(0, idx - 50)
        end = min(len(text), idx + len(q) + 100) if idx >= 0 else 200
        snippet = text[start:end]
        items.append(
            {
                "doc_id": str(doc.id),
                "kb_id": str(doc.kb_id),
                "title": doc.title,
                "snippet": snippet,
                "classification": doc.classification,
                "score": 1.0,
            }
        )

    return ApiResponse(
        code=0,
        data={"results": items, "total": total, "query": q},
        message="success",
    )
