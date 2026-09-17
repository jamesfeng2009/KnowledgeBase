"""
文件夹路由 — 知识库文件夹树的 HTTP 请求/响应转换（P0-3）。

权限模型：所有写操作要求对目标知识库有写权限（PermissionService.check_write），
读操作（tree）要求可读权限（check_function）。

路径约定与 Document.path 对齐：'/'-分隔字符串（"产品/合规"），
None / "" 表示根级。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.knowledge import Document
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.folder import FolderCreate, FolderRename, DocumentMove
from app.services.folder_service import FolderService
from app.services.permission_service import PermissionService
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

router = APIRouter(tags=["文件夹管理"])


def _folder_service(db: AsyncSession, request: Request, user: User) -> FolderService:
    return FolderService(
        db, user, tenant_id=getattr(request.state, "tenant_id", None)
    )


def _perm(db: AsyncSession, request: Request, user: User) -> PermissionService:
    return PermissionService(db, user, tenant_id=getattr(request.state, "tenant_id", None))


@router.get("/knowledge/{kb_id}/folders/tree", response_model=ApiResponse[dict])
async def get_folder_tree(
    kb_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """返回知识库文件夹树（文件夹 + 文档聚合，root 节点）。"""
    perm = _perm(db, request, user)
    if not await perm.check_function(kb_id):
        return ApiResponse(code=403, data=None, message="无知识库访问权限")
    service = _folder_service(db, request, user)
    tree = await service.list_tree(kb_id)
    return ApiResponse(code=0, data=tree, message="success")


@router.post("/knowledge/{kb_id}/folders", response_model=ApiResponse[dict])
async def create_folder(
    kb_id: UUID,
    body: FolderCreate,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """创建文件夹（name + 可选 parent_path）。"""
    perm = _perm(db, request, user)
    if not await perm.check_write(kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    service = _folder_service(db, request, user)
    try:
        folder = await service.create(
            kb_id, name=body.name, parent_path=body.parent_path, sort_order=body.sort_order or 0
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data={"id": str(folder.id), "name": folder.name, "path": folder.path, "depth": folder.depth},
        message="success",
    )


@router.patch("/knowledge/{kb_id}/folders/{folder_id}", response_model=ApiResponse[dict])
async def rename_folder(
    kb_id: UUID,
    folder_id: UUID,
    body: FolderRename,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """重命名文件夹 — 级联更新子文件夹与其中文档的 path。"""
    perm = _perm(db, request, user)
    if not await perm.check_write(kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    service = _folder_service(db, request, user)
    try:
        folder = await service.rename(folder_id, body.name)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data={"id": str(folder.id), "name": folder.name, "path": folder.path, "depth": folder.depth},
        message="success",
    )


@router.delete("/knowledge/{kb_id}/folders/{folder_id}", response_model=ApiResponse)
async def delete_folder(
    kb_id: UUID,
    folder_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """删除文件夹 — 级联软删子文件夹；其中文档移动到父级。"""
    perm = _perm(db, request, user)
    if not await perm.check_write(kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    service = _folder_service(db, request, user)
    try:
        await service.delete(folder_id)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    log.info("api.folder_deleted", folder_id=str(folder_id), kb_id=str(kb_id))
    return ApiResponse(code=0, data=None, message="success")


@router.put("/documents/{doc_id}/folder", response_model=ApiResponse[dict])
async def move_document(
    doc_id: UUID,
    body: DocumentMove,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """把文档移动到指定文件夹（path=None 表示根级）。"""
    # 先取文档归属的知识库用于权限校验
    stmt = select(Document).where(Document.id == doc_id, Document.deleted_at.is_(None))
    stmt = apply_tenant_filter(stmt, Document, getattr(request.state, "tenant_id", None))
    result = await db.execute(stmt)
    doc = result.scalars().first()
    if doc is None:
        return ApiResponse(code=404, data=None, message="文档不存在")
    perm = _perm(db, request, user)
    if not await perm.check_write(doc.kb_id):
        return ApiResponse(code=403, data=None, message="无知识库写权限")
    service = _folder_service(db, request, user)
    try:
        updated = await service.move_document(doc_id, body.path, sort_order=body.sort_order)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data={"id": str(updated.id), "path": updated.path, "depth": updated.depth},
        message="success",
    )
