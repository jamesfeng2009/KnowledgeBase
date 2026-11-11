"""
知识库路由 — 单一职责：处理知识库与文档的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（CRUD、权限校验、密级过滤）委托给 KnowledgeService。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.knowledge import (
    DocCreate,
    DocResponse,
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
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[KbResponse]]:
    """分页查询当前用户可访问的知识库列表。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
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
    request: Request,
    body: KbCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[KbResponse]:
    """创建知识库，当前用户自动成为所有者。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
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
    request: Request,
    kb_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[KbResponse]:
    """获取知识库详情。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    kb = await service.get_kb(kb_id)
    return ApiResponse(
        code=0,
        data=KbResponse.model_validate(kb),
        message="success",
    )


@router.put("/knowledge/{kb_id}", response_model=ApiResponse[KbResponse])
async def update_knowledge_base(
    request: Request,
    kb_id: UUID,
    body: KbUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[KbResponse]:
    """更新知识库信息（仅所有者或 admin）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    update_fields = body.model_dump(exclude_unset=True)
    kb = await service.update_kb(kb_id, **update_fields)
    return ApiResponse(
        code=0,
        data=KbResponse.model_validate(kb),
        message="success",
    )


@router.delete("/knowledge/{kb_id}", response_model=ApiResponse)
async def delete_knowledge_base(
    request: Request,
    kb_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """软删除知识库（仅所有者或 admin）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
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
    request: Request,
    kb_id: UUID,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[DocResponse]]:
    """分页查询指定知识库下的文档列表。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
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
    request: Request,
    kb_id: UUID,
    body: DocCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """向知识库上传/新建文档。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    content_text = body.content_text or ""
    doc = await service.upload_document(
        kb_id=kb_id,
        title=body.title,
        content=content_text,
        doc_type=body.doc_type.value,
        # 纯文本创建无上传文件，按 UTF-8 字节数计入存储配额
        # （留 NULL 会让 BillingService 走"500KB/篇"的估算兜底）
        file_size=len(content_text.encode("utf-8")),
    )
    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )


# ======================================================================
# 文档级 CRUD（GET/PUT/DELETE /documents/{doc_id}）的唯一归属是
# app/api/v1/documents.py。
#
# P0 修复：本模块曾在此处重复定义 GET /documents/{doc_id} 与
# PUT /documents/{doc_id}。由于 api/v1/__init__.py 中 knowledge_router
# 先于 documents_router 注册，FastAPI 只取先注册者，documents.py 中的
# 实现永不执行，导致：
#   1. PUT 请求中的 title/doc_type/status/classification 被静默丢弃
#      （前端标题自动保存返回 200 但库里没改）；
#   2. documents.py 中"更新前版本快照"逻辑失效，版本历史恒为空。
# 切勿在此处重新添加文档级路由。
# ======================================================================
