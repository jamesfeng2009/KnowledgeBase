"""
文档管理路由 — 单一职责：处理文档的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（CRUD、权限校验、文件上传、版本管理）委托给 KnowledgeService。

文档上传支持两种模式：
1. 文本创建（DocCreate JSON 体）；
2. 文件上传（UploadFile，保存到 MinIO，触发 Celery 异步处理）。
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.knowledge import Document, DocumentVersion
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.knowledge import (
    DocResponse,
    DocUpdate,
    DocVersionResponse,
)
from app.services.knowledge_service import KnowledgeService
from app.utils.pagination import PageResult, PaginationParams, paginate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["文档管理"])


# ======================================================================
# 文档 CRUD
# ======================================================================


@router.get("/documents", response_model=ApiResponse[PageResponse[DocResponse]])
async def list_documents(
    kb_id: UUID | None = Query(default=None, description="按知识库过滤"),
    status_filter: str | None = Query(default=None, alias="status", description="按状态过滤"),
    keyword: str | None = Query(default=None, description="标题关键词"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[DocResponse]]:
    """分页查询文档列表（支持知识库、状态、关键词过滤）。"""
    service = KnowledgeService(db, user)
    params = PaginationParams(page=page, size=size)
    allowed = service.permission.allowed_classifications()

    stmt = select(Document).where(
        Document.deleted_at.is_(None),
        Document.classification.in_(allowed),
    )
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    if status_filter is not None:
        stmt = stmt.where(Document.status == status_filter)
    if keyword is not None:
        stmt = stmt.where(Document.title.ilike(f"%{keyword}%"))

    # 非 admin 仅可见可访问知识库的文档
    if user.role != "admin":
        from app.models.user import KbMember

        member_subq = select(KbMember.kb_id).where(KbMember.user_id == user.id)
        from app.models.knowledge import KnowledgeBase

        owned_subq = select(KnowledgeBase.id).where(
            KnowledgeBase.owner_id == user.id,
            KnowledgeBase.deleted_at.is_(None),
        )
        stmt = stmt.where(
            Document.kb_id.in_(member_subq) | Document.kb_id.in_(owned_subq)
        )

    stmt = stmt.order_by(Document.created_at.desc())
    result: PageResult = await paginate(stmt, params, db)

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


@router.post("/documents/upload", response_model=ApiResponse[DocResponse], status_code=201)
async def upload_document_file(
    kb_id: UUID = Query(..., description="目标知识库 ID"),
    title: str = Query(..., min_length=1, max_length=500, description="文档标题"),
    file: UploadFile = File(..., description="上传的文件"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """上传文档文件（保存到 MinIO，触发 Celery 异步处理）。

    支持的文件类型：md / html / docx / pdf。
    文件内容先存入 MinIO，再创建 Document 记录，最后通过 Celery 异步解析。
    """
    service = KnowledgeService(db, user)

    # 根据文件扩展名推断文档类型
    filename = file.filename or "unknown"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "md"
    doc_type_map = {"md": "md", "html": "html", "docx": "docx", "pdf": "pdf"}
    doc_type = doc_type_map.get(ext, "md")

    # 读取文件内容
    content_bytes = await file.read()

    # 尝试解码为文本（二进制格式如 docx/pdf 无法直接解码）
    try:
        content_text = content_bytes.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        content_text = ""

    # 尝试上传到 MinIO
    file_path = None
    try:
        from app.utils.minio_client import upload_file  # type: ignore[import-not-found]

        file_path = await upload_file(
            bucket="ekb-documents",
            object_name=f"{kb_id}/{title}",
            data=content_bytes,
            content_type=file.content_type,
        )
    except ImportError:
        logger.debug("MinIO 客户端未安装，文件路径仅记录文件名")
        file_path = f"minio://ekb-documents/{kb_id}/{title}"
    except Exception:
        logger.exception("MinIO 上传失败")
        file_path = f"local://{filename}"

    # 创建文档记录
    doc = await service.upload_document(
        kb_id=kb_id,
        title=title,
        content=content_text,
        doc_type=doc_type,
    )

    # 更新 file_path
    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db)
    await doc_repo.update(doc.id, file_path=file_path)

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
    """更新文档内容（支持协同编辑场景）。

    更新前自动保存当前内容为版本快照（用于版本回溯）。
    """
    service = KnowledgeService(db, user)

    # 获取更新前的文档（用于版本快照）
    old_doc = await service.get_document(doc_id)

    # 保存版本快照
    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db)
    await doc_repo.session.execute(
        DocumentVersion.__table__.insert().values(
            doc_id=old_doc.id,
            content_html=old_doc.content_html,
            content_json=old_doc.content_json,
            author_id=user.id,
            summary=f"更新前快照 - {body.title or old_doc.title}",
        )
    )
    await doc_repo.session.flush()

    # 执行更新
    update_fields = body.model_dump(exclude_unset=True)
    doc = await service.update_document(
        doc_id,
        content_html=update_fields.get("content_html"),
        content_json=update_fields.get("content_json"),
        content_text=update_fields.get("content_text"),
    )

    # 更新其他字段
    extra_fields = {
        k: v for k, v in update_fields.items()
        if k in ("title", "doc_type", "status", "classification") and v is not None
    }
    if extra_fields:
        updated = await doc_repo.update(doc_id, **extra_fields)
        if updated is not None:
            doc = updated

    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )


@router.delete("/documents/{doc_id}", response_model=ApiResponse)
async def delete_document(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """软删除文档（仅所有者或 admin 可操作）。"""
    service = KnowledgeService(db, user)
    doc = await service.get_document(doc_id)

    if doc.owner_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅所有者或管理员可删除文档",
        )

    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db)
    await doc_repo.soft_delete(doc_id)
    return ApiResponse(code=0, message="success")


# ======================================================================
# 文档图片上传
# ======================================================================


@router.post(
    "/documents/{doc_id}/upload-image",
    response_model=ApiResponse[dict],
)
async def upload_document_image(
    doc_id: UUID,
    file: UploadFile = File(..., description="图片文件"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """上传文档内图片到 MinIO，返回图片 URL。

    支持 PNG / JPG / GIF / WEBP 格式。
    """
    service = KnowledgeService(db, user)
    doc = await service.get_document(doc_id)

    # 校验文件类型
    allowed_types = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的图片类型: {file.content_type}",
        )

    content_bytes = await file.read()
    ext_map = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    ext = ext_map.get(file.content_type, "png")
    object_name = f"images/{doc_id}/{file.filename or f'image.{ext}'}"

    image_url: str
    try:
        from app.utils.minio_client import upload_file  # type: ignore[import-not-found]

        image_url = await upload_file(
            bucket="ekb-documents",
            object_name=object_name,
            data=content_bytes,
            content_type=file.content_type,
        )
    except ImportError:
        logger.debug("MinIO 客户端未安装，返回占位 URL")
        image_url = f"/uploads/{object_name}"
    except Exception:
        logger.exception("MinIO 图片上传失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="图片上传失败",
        )

    return ApiResponse(
        code=0,
        data={"url": image_url, "filename": file.filename},
        message="success",
    )


# ======================================================================
# 文档版本管理
# ======================================================================


@router.get(
    "/documents/{doc_id}/versions",
    response_model=ApiResponse[list[DocVersionResponse]],
)
async def list_document_versions(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[DocVersionResponse]]:
    """获取文档版本历史。"""
    service = KnowledgeService(db, user)
    await service.get_document(doc_id)

    stmt = (
        select(DocumentVersion)
        .where(DocumentVersion.doc_id == doc_id)
        .order_by(DocumentVersion.created_at.desc())
    )
    result = await db.execute(stmt)
    versions = list(result.scalars().all())

    return ApiResponse(
        code=0,
        data=[DocVersionResponse.model_validate(v) for v in versions],
        message="success",
    )


@router.post(
    "/documents/{doc_id}/versions/{ver_id}/restore",
    response_model=ApiResponse[DocResponse],
)
async def restore_document_version(
    doc_id: UUID,
    ver_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """恢复文档到指定历史版本。

    将历史版本的内容写回文档，并保存当前内容为新版本快照。
    """
    service = KnowledgeService(db, user)
    doc = await service.get_document(doc_id)

    # 查询目标版本
    stmt = select(DocumentVersion).where(
        DocumentVersion.id == ver_id,
        DocumentVersion.doc_id == doc_id,
    )
    result = await db.execute(stmt)
    version = result.scalars().first()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 {ver_id} 不存在",
        )

    # 保存当前内容为版本快照
    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db)
    await doc_repo.session.execute(
        DocumentVersion.__table__.insert().values(
            doc_id=doc.id,
            content_html=doc.content_html,
            content_json=doc.content_json,
            author_id=user.id,
            summary=f"恢复前快照 - 恢复到 {ver_id}",
        )
    )
    await doc_repo.session.flush()

    # 恢复版本内容
    restored = await doc_repo.update(
        doc_id,
        content_html=version.content_html,
        content_json=version.content_json,
    )
    if restored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {doc_id} 不存在",
        )

    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(restored),
        message="success",
    )
