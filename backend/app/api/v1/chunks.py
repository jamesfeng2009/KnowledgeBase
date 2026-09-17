"""
分块路由 — 检索分块编辑与版本历史的 HTTP 请求/响应转换（P0-2）。

权限模型：
    - 读（list / versions / version 详情）：要求对文档所属知识库有读权限；
    - 写（init / update / rollback）：要求有写权限。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.chunk import ChunkEdit
from app.schemas.common import ApiResponse
from app.services.chunk_service import ChunkService
from app.services.permission_service import PermissionService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["分块管理"])


def _chunk_service(db: AsyncSession, request: Request, user: User) -> ChunkService:
    return ChunkService(db, user, tenant_id=getattr(request.state, "tenant_id", None))


def _perm(db: AsyncSession, request: Request, user: User) -> PermissionService:
    return PermissionService(db, user, tenant_id=getattr(request.state, "tenant_id", None))


def _chunk_dict(chunk) -> dict:  # noqa: ANN001
    return {
        "id": str(chunk.id),
        "doc_id": str(chunk.doc_id),
        "chunk_index": chunk.chunk_index,
        "section_path": chunk.section_path,
        "content": chunk.content_text,
        "token_count": chunk.token_count,
    }


def _version_dict(version) -> dict:  # noqa: ANN001
    return {
        "id": str(version.id),
        "version_seq": version.version_seq,
        "content": version.content_text,
        "summary": version.summary,
        "author_id": str(version.author_id),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


@router.get("/documents/{doc_id}/chunks", response_model=ApiResponse[dict])
async def list_document_chunks(
    doc_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """列出文档的持久化分块（按 chunk_index 升序）。

    若文档尚未 init，返回空列表（前端可引导调用 init）。
    """
    service = _chunk_service(db, request, user)
    doc = await service.get_document(doc_id)
    if doc is None:
        return ApiResponse(code=404, data=None, message="文档不存在")
    perm = _perm(db, request, user)
    if not await perm.check_function(doc.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    chunks = await service.list_chunks(doc_id)
    return ApiResponse(
        code=0,
        data={"items": [_chunk_dict(c) for c in chunks], "total": len(chunks)},
        message="success",
    )


@router.post("/documents/{doc_id}/chunks/init", response_model=ApiResponse[dict])
async def init_document_chunks(
    doc_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """初始化文档分块 — 幂等：已有分块直接返回。"""
    service = _chunk_service(db, request, user)
    doc = await service.get_document(doc_id)
    if doc is None:
        return ApiResponse(code=404, data=None, message="文档不存在")
    perm = _perm(db, request, user)
    if not await perm.check_write(doc.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    try:
        chunks = await service.init_chunks(doc_id)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data={"items": [_chunk_dict(c) for c in chunks], "total": len(chunks)},
        message="success",
    )


@router.put("/documents/{doc_id}/chunks/{chunk_id}", response_model=ApiResponse[dict])
async def update_chunk(
    doc_id: UUID,
    chunk_id: UUID,
    body: ChunkEdit,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """编辑分块正文 — 快照旧内容 + 重建文档索引。"""
    service = _chunk_service(db, request, user)
    doc = await service.get_document(doc_id)
    if doc is None:
        return ApiResponse(code=404, data=None, message="文档不存在")
    perm = _perm(db, request, user)
    if not await perm.check_write(doc.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    try:
        chunk = await service.update_chunk(
            chunk_id, content=body.content, author_id=user.id, summary=body.summary
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=_chunk_dict(chunk), message="success")


@router.get("/chunks/{chunk_id}/versions", response_model=ApiResponse[dict])
async def list_chunk_versions(
    chunk_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """列出分块版本历史（版本号降序）。"""
    service = _chunk_service(db, request, user)
    chunk = await service.get_chunk(chunk_id)
    if chunk is None:
        return ApiResponse(code=404, data=None, message="分块不存在")
    doc = await service.get_document(chunk.doc_id)
    if doc is None:
        return ApiResponse(code=404, data=None, message="文档不存在")
    perm = _perm(db, request, user)
    if not await perm.check_function(doc.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    versions = await service.list_versions(chunk_id)
    return ApiResponse(
        code=0,
        data={"items": [_version_dict(v) for v in versions], "total": len(versions)},
        message="success",
    )


@router.get("/chunks/{chunk_id}/versions/{version_id}", response_model=ApiResponse[dict])
async def get_chunk_version(
    chunk_id: UUID,
    version_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """返回分块版本详情（含相对当前正文的 unified diff）。"""
    service = _chunk_service(db, request, user)
    chunk = await service.get_chunk(chunk_id)
    if chunk is None:
        return ApiResponse(code=404, data=None, message="分块不存在")
    doc = await service.get_document(chunk.doc_id)
    if doc is None:
        return ApiResponse(code=404, data=None, message="文档不存在")
    perm = _perm(db, request, user)
    if not await perm.check_function(doc.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    try:
        version, diff_lines = await service.get_version(chunk_id, version_id)
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))
    data = _version_dict(version)
    data["diff"] = diff_lines
    return ApiResponse(code=0, data=data, message="success")


@router.post(
    "/chunks/{chunk_id}/versions/{version_id}/rollback",
    response_model=ApiResponse[dict],
)
async def rollback_chunk(
    chunk_id: UUID,
    version_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """回滚分块到指定版本 — 快照当前 + 写回版本内容 + 重建索引。"""
    service = _chunk_service(db, request, user)
    chunk = await service.get_chunk(chunk_id)
    if chunk is None:
        return ApiResponse(code=404, data=None, message="分块不存在")
    doc = await service.get_document(chunk.doc_id)
    if doc is None:
        return ApiResponse(code=404, data=None, message="文档不存在")
    perm = _perm(db, request, user)
    if not await perm.check_write(doc.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    try:
        updated = await service.rollback(chunk_id, version_id, author_id=user.id)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=_chunk_dict(updated), message="success")
