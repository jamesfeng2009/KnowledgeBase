"""
知识库路由 — 单一职责：处理知识库与文档的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（CRUD、权限校验、密级过滤）委托给 KnowledgeService。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.knowledge import (
    DocCreate,
    DocResponse,
    DocUpdate,
    KbCreate,
    KbResponse,
    KbUpdate,
)
from app.services.knowledge_service import KnowledgeService

router = APIRouter(tags=["知识库管理"])


# ======================================================================
# 知识库 CRUD
# ======================================================================


@router.get("/knowledge", response_model=ApiResponse[PageResponse[KbResponse]])
async def list_knowledge_bases(
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[KbResponse]]:
    """分页查询当前用户可访问的知识库列表。"""
    service = KnowledgeService(db, user)
    result = await service.list_kbs(page=page, size=size)
    return ApiResponse(
        code=0,
        data=PageResponse[KbResponse](
            items=[KbResponse.model_validate(kb) for kb in result.items],
            total=result.total,
            page=result.page,
            size=result.size,
            pages=result.pages,
        ),
        message="success",
    )


@router.post("/knowledge", response_model=ApiResponse[KbResponse], status_code=201)
async def create_knowledge_base(
    body: KbCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[KbResponse]:
    """创建知识库，当前用户自动成为所有者。"""
    service = KnowledgeService(db, user)
    kb = await service.create_kb(
        name=body.name,
        description=body.description,
        visibility=body.visibility.value,
    )
    return ApiResponse(
        code=0,
        data=KbResponse.model_validate(kb),
        message="success",
    )


@router.get("/knowledge/{kb_id}", response_model=ApiResponse[KbResponse])
async def get_knowledge_base(
    kb_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[KbResponse]:
    """获取知识库详情。"""
    service = KnowledgeService(db, user)
    kb = await service.get_kb(kb_id)
    return ApiResponse(
        code=0,
        data=KbResponse.model_validate(kb),
        message="success",
    )


@router.put("/knowledge/{kb_id}", response_model=ApiResponse[KbResponse])
async def update_knowledge_base(
    kb_id: UUID,
    body: KbUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[KbResponse]:
    """更新知识库信息（仅所有者或 admin）。"""
    service = KnowledgeService(db, user)
    update_fields = body.model_dump(exclude_unset=True)
    kb = await service.update_kb(kb_id, **update_fields)
    return ApiResponse(
        code=0,
        data=KbResponse.model_validate(kb),
        message="success",
    )


@router.delete("/knowledge/{kb_id}", response_model=ApiResponse)
async def delete_knowledge_base(
    kb_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """软删除知识库（仅所有者或 admin）。"""
    service = KnowledgeService(db, user)
    await service.delete_kb(kb_id)
    return ApiResponse(code=0, message="success")


# ======================================================================
# 文档管理
# ======================================================================


@router.get(
    "/knowledge/{kb_id}/documents",
    response_model=ApiResponse[PageResponse[DocResponse]],
)
async def list_documents(
    kb_id: UUID,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[DocResponse]]:
    """分页查询指定知识库下的文档列表。"""
    service = KnowledgeService(db, user)
    result = await service.list_documents(kb_id=kb_id, page=page, size=size)
    return ApiResponse(
        code=0,
        data=PageResponse[DocResponse](
            items=[DocResponse.model_validate(doc) for doc in result.items],
            total=result.total,
            page=result.page,
            size=result.size,
            pages=result.pages,
        ),
        message="success",
    )


@router.post(
    "/knowledge/{kb_id}/documents",
    response_model=ApiResponse[DocResponse],
    status_code=201,
)
async def upload_document(
    kb_id: UUID,
    body: DocCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """向知识库上传/新建文档。"""
    service = KnowledgeService(db, user)
    doc = await service.upload_document(
        kb_id=kb_id,
        title=body.title,
        content=body.content_text or "",
        doc_type=body.doc_type.value,
    )
    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )


@router.get("/documents/{doc_id}", response_model=ApiResponse[DocResponse])
async def get_document(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """获取文档详情。"""
    service = KnowledgeService(db, user)
    doc = await service.get_document(doc_id)
    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )


@router.put("/documents/{doc_id}", response_model=ApiResponse[DocResponse])
async def update_document(
    doc_id: UUID,
    body: DocUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """更新文档内容（支持协同编辑场景）。"""
    service = KnowledgeService(db, user)
    doc = await service.update_document(
        doc_id,
        content_html=body.content_html,
        content_json=body.content_json,
        content_text=body.content_text,
    )
    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )
