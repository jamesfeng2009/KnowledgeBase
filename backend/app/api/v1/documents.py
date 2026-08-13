"""
文档管理路由 — 单一职责：处理文档的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（CRUD、权限校验、文件上传、版本管理）委托给 KnowledgeService。

文档上传支持两种模式：
1. 文本创建（DocCreate JSON 体）；
2. 文件上传（UploadFile，保存到 MinIO，触发 Celery 异步处理）。

P0 增强：文件大小校验（MAX_UPLOAD_SIZE_MB）— 超限返回 413 Payload Too Large。
P1 增强：解析摘要响应（/documents/{doc_id}/summary）— 返回 preview/structure/warnings/pages。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.knowledge import Document, DocumentVersion
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.knowledge import (
    DocResponse,
    DocUpdate,
    DocVersionResponse,
    DocumentImportRequest,
    DocumentImportResponse,
    DocumentSummaryResponse,
)
from app.services.knowledge_service import KnowledgeService
from app.utils.logger import get_logger
from app.utils.pagination import PageResult, PaginationParams, paginate

log = get_logger(__name__)

router = APIRouter(tags=["文档管理"])

# 上传流式读取分块大小（1MB）— 避免一次性全量读入内存
_UPLOAD_READ_CHUNK_BYTES = 1 * 1024 * 1024
# Content-Length 预检的 multipart 表单开销余量（边界/表单字段/文件名等）
_MULTIPART_FORM_OVERHEAD_BYTES = 1 * 1024 * 1024


def _max_upload_bytes() -> int:
    """读取上传大小上限（字节）。

    读取 ``settings.MAX_UPLOAD_SIZE_MB``（默认 50MB）；
    配置缺失、非 int 或 <= 0 时回退 50MB（兼容 MagicMock 测试场景）。
    """
    settings = get_settings()
    max_mb = getattr(settings, "MAX_UPLOAD_SIZE_MB", 50)
    if not isinstance(max_mb, int) or max_mb <= 0:
        max_mb = 50
    return max_mb * 1024 * 1024


def _raise_upload_too_large(max_bytes: int) -> None:
    """抛出 413 — 文件大小超过上限。"""
    max_mb = max_bytes // (1024 * 1024)
    log.warning("upload.size_exceeded", max_mb=max_mb)
    raise HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=(
            f"文件大小超过上限 {max_mb}MB，"
            f"请压缩后上传或联系管理员调整 MAX_UPLOAD_SIZE_MB"
        ),
    )


def _check_content_length(request: Request) -> None:
    """Content-Length 预检 — 内存 DoS 防护第一道闸门。

    在任何正文读取之前执行：若声明的请求体大小已确定超过
    上限 + multipart 表单开销余量，直接 413 拒绝，不读取任何字节。
    未声明 Content-Length（如 chunked 传输）时跳过，
    由 ``_read_upload_bounded`` 的分块计数兜底。
    """
    content_length = request.headers.get("content-length")
    if not content_length or not content_length.isdigit():
        return
    max_bytes = _max_upload_bytes()
    if int(content_length) > max_bytes + _MULTIPART_FORM_OVERHEAD_BYTES:
        _raise_upload_too_large(max_bytes)


async def _read_upload_bounded(file: UploadFile) -> bytes:
    """分块流式读取上传内容并累计计数 — 内存 DoS 防护第二道闸门。

    每读一块即校验累计大小，超限立即 413 拒绝：
    内存占用最多为 上限 + 1 个分块，不会先把超大文件全量读入内存。

    Returns:
        文件完整二进制内容（未超限时）。

    Raises:
        HTTPException: 413 Payload Too Large 当累计大小超过配置上限。
    """
    max_bytes = _max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            log.warning(
                "upload.stream_size_exceeded", total_bytes=total, max_bytes=max_bytes
            )
            _raise_upload_too_large(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_upload_size(content_bytes: bytes) -> None:
    """校验上传文件大小 — 超过 MAX_UPLOAD_SIZE_MB 返回 413。

    Args:
        content_bytes: 文件二进制内容。

    Raises:
        HTTPException: 413 Payload Too Large 当文件超过配置上限。
    """
    max_bytes = _max_upload_bytes()
    if len(content_bytes) > max_bytes:
        _raise_upload_too_large(max_bytes)


async def _check_kb_write_access(
    db: AsyncSession,
    user: User,
    kb_id: UUID,
    tenant_id: UUID | None,
) -> None:
    """校验当前用户是否拥有目标知识库的写权限（安全 — 防 IDOR）。

    知识库不存在、跨租户不可见或用户无写权限时统一拒绝。

    Raises:
        HTTPException: 403 — 无权向该知识库上传文档。
    """
    from app.services.permission_service import PermissionService

    permission = PermissionService(db, user, tenant_id=tenant_id)
    if not await permission.check_write(kb_id):
        raise HTTPException(status_code=403, detail="无权向该知识库上传文档")


async def _load_multipart_session(upload_id: str) -> dict | None:
    """读取 Redis 中的多段上传会话元数据（不存在或异常返回 None）。

    使用 redis.asyncio 客户端 — 调用方均为 async 上下文，
    同步客户端会阻塞事件循环。
    """
    import json

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            raw = await client.get(f"ekb:multipart:{upload_id}")
        finally:
            await client.aclose()
        return json.loads(raw) if raw else None
    except Exception:
        log.debug("multipart.session_load_failed", exc_info=True)
        return None


async def _delete_multipart_session(upload_id: str) -> None:
    """清理 Redis 中的多段上传会话（异步客户端，不阻塞事件循环）。"""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            await client.delete(f"ekb:multipart:{upload_id}")
        finally:
            await client.aclose()
    except Exception:
        log.debug("multipart.session_delete_failed", exc_info=True)


def _check_multipart_session(
    session: dict | None,
    user: User,
    object_name: str | None = None,
) -> None:
    """校验多段上传会话归属与对象路径绑定（安全 — 防 IDOR）。

    - 会话不存在 / 已过期 → 404；
    - 会话属于其他用户（非 admin）→ 403；
    - object_name 与会话记录不符（防止借用自有会话写入他人命名空间）→ 403。
    """
    if session is None:
        raise HTTPException(status_code=404, detail="上传会话不存在或已过期")
    if user.role != "admin" and session.get("user_id") != str(user.id):
        raise HTTPException(status_code=403, detail="无权操作他人的上传会话")
    if object_name and session.get("object_name") and session["object_name"] != object_name:
        raise HTTPException(status_code=403, detail="object_name 与上传会话不匹配")


# ======================================================================
# 文档 CRUD
# ======================================================================


@router.get("/documents", response_model=ApiResponse[PageResponse[DocResponse]])
async def list_documents(
    request: Request,
    kb_id: UUID | None = Query(default=None, description="按知识库过滤"),
    status_filter: str | None = Query(default=None, alias="status", description="按状态过滤"),
    keyword: str | None = Query(default=None, description="标题关键词"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[DocResponse]]:
    """分页查询文档列表（支持知识库、状态、关键词过滤）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    params = PaginationParams(page=page, size=size)
    allowed = service.permission.allowed_classifications()

    stmt = select(Document).where(
        Document.deleted_at.is_(None),
        Document.classification.in_(allowed),
    )
    # 多租户隔离（安全）：仅返回当前租户的文档，杜绝跨租户数据可见
    if tenant_id is not None:
        stmt = stmt.where(Document.tenant_id == tenant_id)
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
    request: Request,
    kb_id: UUID = Query(..., description="目标知识库 ID"),
    title: str = Query(..., min_length=1, max_length=500, description="文档标题"),
    file: UploadFile = File(..., description="上传的文件"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """上传文档文件（保存到 MinIO，触发 Celery 异步处理）。

    支持的文件类型：md / html / docx / pdf / pptx / xlsx / xls / txt / csv。
    文件内容先存入 MinIO，再创建 Document 记录，最后通过 Celery 异步解析。

    P0: 文件大小校验 — 超过 MAX_UPLOAD_SIZE_MB 返回 413。
    P0: 上传后自动触发 process_document Celery 任务（修复原端点未调用的缺陷）。
    P0: doc_type_map 补全 pptx/xlsx/xls/txt/csv（修复原映射缺失导致误归 md）。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    # 安全：先校验知识库写权限再做任何文件 IO（防 IDOR，与 multipart init 端点一致），
    # 避免未授权用户向他人知识库命名空间写入 MinIO 对象
    await _check_kb_write_access(db, user, kb_id, tenant_id)
    service = KnowledgeService(db, user, tenant_id=tenant_id)

    # 根据文件扩展名推断文档类型
    # P0 修复：补全所有受支持的格式，避免 pptx/xlsx 被误归为 md
    filename = file.filename or "unknown"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "md"
    doc_type_map = {
        "md": "md",
        "markdown": "md",
        "html": "html",
        "htm": "html",
        "docx": "docx",
        "pdf": "pdf",
        "pptx": "pptx",
        "xlsx": "xlsx",
        "xls": "xls",
        "txt": "txt",
        "csv": "csv",
    }
    doc_type = doc_type_map.get(ext, "md")

    # P0 安全修复（内存 DoS 防护）：先校验大小再读取 —
    # 1) Content-Length 预检：声明大小超限直接 413，不读取任何字节；
    # 2) 分块流式读取 + 累计计数：超限即拒，绝不先全量读入内存。
    _check_content_length(request)
    content_bytes = await _read_upload_bounded(file)

    # P0-2 存储配额强制：上传前基于租户已用存储预检，超限抛 403。
    # tenant_id 为 None（私有部署/单租户）时跳过配额。
    if tenant_id is not None:
        from app.services.billing_service import BillingService, QuotaExceededError

        billing = BillingService(db)
        try:
            await billing.check_storage_quota(
                tenant_id, additional_bytes=len(content_bytes)
            )
        except QuotaExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"存储配额不足：{exc}",
            ) from exc

    # 尝试解码为文本（二进制格式如 docx/pdf 无法直接解码）
    try:
        content_text = content_bytes.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        content_text = ""

    # 尝试上传到 MinIO
    file_path = None
    object_name = f"{kb_id}/{title}"
    minio_uploaded = False
    try:
        from app.utils.minio_client import upload_file

        file_path = await upload_file(
            bucket="ekb-documents",
            object_name=object_name,
            data=content_bytes,
            content_type=file.content_type,
        )
        minio_uploaded = True
    except ImportError:
        log.debug("MinIO 客户端未安装，文件路径仅记录文件名")
        file_path = f"minio://ekb-documents/{kb_id}/{title}"
    except Exception:
        log.exception("MinIO 上传失败")
        file_path = f"local://{filename}"

    # 创建文档记录（service 层校验知识库写权限；PermissionError 映射为 403）
    try:
        doc = await service.upload_document(
            kb_id=kb_id,
            title=title,
            content=content_text,
            doc_type=doc_type,
        )
    except Exception as exc:
        # 补偿：文档创建失败时删除已上传的 MinIO 对象，避免遗留孤儿文件；
        # 删除本身再失败仅记录日志，不掩盖原始异常
        if minio_uploaded:
            try:
                from app.utils.minio_client import delete_file

                await delete_file(bucket="ekb-documents", object_name=object_name)
            except Exception:
                log.warning("MinIO 孤儿对象补偿删除失败: %s", object_name)
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise

    # 更新 file_path（租户隔离的 Repository）
    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db, tenant_id=tenant_id)
    await doc_repo.update(doc.id, file_path=file_path)

    # P0 修复：上传成功后立即触发 Celery 异步解析任务
    # 原端点只存了 MinIO + 创建记录，未调用 process_document.delay()，
    # 导致上传后文档永远停留在 draft 状态，无法被搜索和使用。
    try:
        from tasks.document_tasks import process_document

        process_document.delay(str(doc.id), tenant_id=str(tenant_id) if tenant_id else None)
        log.info("文档 %s 已触发 Celery 异步解析任务", doc.id)
    except ImportError:
        log.warning(
            "Celery 任务模块未安装，文档 %s 不会自动解析，需手动触发", doc.id
        )
    except Exception:
        # Celery 不可用时不应阻断上传响应，仅记录日志
        log.exception("触发 Celery 解析任务失败，文档 %s 需手动处理", doc.id)

    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )


# ======================================================================
# P2-A 多段上传 — 突破 50MB 限制，支持 GB 级视频
# ======================================================================


@router.post("/documents/multipart/init", response_model=ApiResponse[dict])
async def init_multipart_upload(
    request: Request,
    kb_id: UUID = Query(..., description="目标知识库 ID"),
    title: str = Query(..., min_length=1, max_length=500, description="文档标题"),
    filename: str = Query(..., description="原始文件名（用于推断 doc_type）"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """初始化多段上传 — 返回 upload_id（P2-A）。

    前端发起 GB 级视频上传时调用此端点，获取 upload_id 后逐片上传。
    upload_id 同时存入 Redis（TTL 24h），用于断点续传校验。

    安全：初始化前校验当前用户对目标知识库的写权限（防 IDOR），
    防止向他人知识库命名空间写入分片。

    Returns:
        {"upload_id": "xxx", "object_name": "kb_id/title"}
    """
    import json
    import time

    tenant_id = getattr(request.state, "tenant_id", None)
    await _check_kb_write_access(db, user, kb_id, tenant_id)

    object_name = f"{kb_id}/{title}"

    # 先调用 MinIO 初始化多段上传,拿到 minio_upload_id
    try:
        from app.utils.minio_client import init_multipart_upload as _init

        minio_upload_id = await _init(
            bucket="ekb-documents",
            object_name=object_name,
        )
    except ImportError:
        log.warning("multipart.minio_not_installed")
        raise HTTPException(503, detail="MinIO 未安装，不支持多段上传")
    except Exception:
        log.exception("multipart.init_failed")
        raise HTTPException(500, detail="初始化多段上传失败")

    # 在 Redis 记录会话元数据（用于孤儿分片清理和断点续传）
    # P1 加固：用 minio_upload_id 作为 key（与前端后续操作使用的 ID 一致），
    # session 包含 minio_upload_id，供清理任务调用 abort_multipart_upload。
    # 使用 redis.asyncio — 同步客户端会阻塞事件循环。
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            get_settings().REDIS_URL, decode_responses=True
        )
        session = {
            "kb_id": str(kb_id),
            "title": title,
            "filename": filename,
            "object_name": object_name,
            "user_id": str(user.id),
            "minio_upload_id": minio_upload_id,
            "created_at": time.time(),
            "status": "initiated",
        }
        try:
            await client.setex(
                f"ekb:multipart:{minio_upload_id}",
                86400,  # 24h TTL
                json.dumps(session, ensure_ascii=False),
            )
        finally:
            await client.aclose()
    except Exception:
        log.debug("multipart.session_redis_failed", upload_id=minio_upload_id)

    return ApiResponse(
        code=0,
        data={"upload_id": minio_upload_id, "object_name": object_name},
        message="success",
    )


@router.put("/documents/multipart/{upload_id}/parts/{part_number}")
async def upload_part(
    upload_id: str,
    part_number: int,
    object_name: str = Query(..., description="对象存储路径"),
    file: UploadFile = File(..., description="分片内容"),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """上传单个分片（P2-A）。

    分片编号从 1 开始（S3 协议约定）。每片建议 5-10MB。
    返回 etag，前端需保存以便 complete 时提交。

    Returns:
        {"part_number": 1, "etag": "abc123"}
    """
    if part_number < 1 or part_number > 10000:
        raise HTTPException(400, detail="part_number 必须在 1-10000 之间")

    # 安全：校验上传会话归属与 object_name 绑定（防 IDOR —
    # 阻止向他人上传会话注入分片，进而污染他人文档内容）
    _check_multipart_session(
        await _load_multipart_session(upload_id), user, object_name
    )

    # 读取分片内容（单片 ≤ 10MB，内存安全）
    data = await file.read()

    try:
        from app.utils.minio_client import upload_part as _upload_part

        result = await _upload_part(
            bucket="ekb-documents",
            object_name=object_name,
            upload_id=upload_id,
            part_number=part_number,
            data=data,
        )
        return ApiResponse(code=0, data=result, message="success")
    except ImportError:
        raise HTTPException(503, detail="MinIO 未安装")
    except Exception:
        log.exception("multipart.upload_part_failed", part_number=part_number)
        raise HTTPException(500, detail=f"分片 {part_number} 上传失败")


@router.post("/documents/multipart/{upload_id}/complete", response_model=ApiResponse[DocResponse])
async def complete_multipart_upload(
    request: Request,
    upload_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """合并分片并创建文档记录（P2-A）。

    Request body::
        {
            "parts": [{"part_number": 1, "etag": "..."}, ...],
            "object_name": "kb_id/title",
            "kb_id": "uuid",
            "title": "文档标题",
            "doc_type": "mp4"
        }

    Returns:
        文档记录（DocResponse）— 创建后自动触发 Celery 解析。
    """
    parts = payload.get("parts", [])
    object_name = payload.get("object_name", "")
    kb_id_str = payload.get("kb_id", "")
    title = payload.get("title", "")
    doc_type = payload.get("doc_type", "md")

    if not parts or not object_name or not kb_id_str or not title:
        raise HTTPException(400, detail="缺少必要参数 parts/object_name/kb_id/title")

    # 安全：校验上传会话归属与 object_name 绑定（防 IDOR —
    # 阻止合并他人上传会话或借用自有会话写入他人命名空间）
    _check_multipart_session(
        await _load_multipart_session(upload_id), user, object_name
    )

    # P0: 完整性校验 — 防止分片乱序、缺片、etag 不匹配导致视频损坏
    sorted_parts = sorted(parts, key=lambda p: p.get("part_number", 0))
    part_numbers = [p.get("part_number") for p in sorted_parts]

    # 校验 1: part_number 从 1 开始连续无缺
    expected = list(range(1, len(parts) + 1))
    if part_numbers != expected:
        missing = sorted(set(expected) - set(part_numbers))
        raise HTTPException(
            400,
            detail={
                "error": "parts_not_continuous",
                "message": f"分片编号不连续，缺失: {missing}",
                "missing_parts": missing,
                "received_parts": part_numbers,
            },
        )

    # 校验 2: 每个分片必须有 etag
    empty_etag_parts = [p["part_number"] for p in sorted_parts if not p.get("etag")]
    if empty_etag_parts:
        raise HTTPException(
            400,
            detail={
                "error": "etag_missing",
                "message": f"分片缺少 etag: {empty_etag_parts}",
                "missing_etag_parts": empty_etag_parts,
            },
        )

    # 校验 3: 调用 list_parts 对账服务端实际已上传分片
    try:
        from app.utils.minio_client import list_parts

        server_parts = await list_parts(
            bucket="ekb-documents",
            object_name=object_name,
            upload_id=upload_id,
        )
        server_part_numbers = {p.get("part_number") for p in server_parts}
        client_part_numbers = set(part_numbers)

        # 客户端声称上传但服务端没有的分片
        client_only = sorted(client_part_numbers - server_part_numbers)
        if client_only:
            raise HTTPException(
                400,
                detail={
                    "error": "parts_lost_on_server",
                    "message": f"分片在服务端不存在，需重新上传: {client_only}",
                    "missing_parts": client_only,
                },
            )

        # etag 不匹配的分片（数据损坏）
        server_etag_map = {p.get("part_number"): p.get("etag") for p in server_parts}
        etag_mismatch = []
        for p in sorted_parts:
            pn = p["part_number"]
            if server_etag_map.get(pn) and server_etag_map[pn] != p.get("etag"):
                etag_mismatch.append(pn)
        if etag_mismatch:
            raise HTTPException(
                400,
                detail={
                    "error": "etag_mismatch",
                    "message": f"分片 etag 与服务端不匹配（数据损坏）: {etag_mismatch}",
                    "mismatch_parts": etag_mismatch,
                },
            )
    except HTTPException:
        raise
    except Exception:
        log.warning("multipart.list_parts_check_failed", exc_info=True)
        # list_parts 失败不阻断合并（降级），仅记录日志

    # 1. 调用 MinIO 合并分片
    try:
        from app.utils.minio_client import complete_multipart_upload as _complete

        file_path = await _complete(
            bucket="ekb-documents",
            object_name=object_name,
            upload_id=upload_id,
            parts=sorted_parts,  # P0: 使用排序后的 parts，确保 MinIO 按正确顺序合并
        )
    except ImportError:
        raise HTTPException(503, detail="MinIO 未安装")
    except Exception:
        log.exception("multipart.complete_failed")
        raise HTTPException(500, detail="合并分片失败")

    # 2. 创建文档记录
    try:
        kb_id = UUID(kb_id_str)
    except ValueError:
        raise HTTPException(400, detail="kb_id 格式错误")

    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    try:
        doc = await service.upload_document(
            kb_id=kb_id,
            title=title,
            content="",  # 视频文档无文本内容，由 Celery ASR 转写填充
            doc_type=doc_type,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # 3. 更新 file_path 指向 MinIO 合并后的对象（租户隔离的 Repository）
    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db, tenant_id=tenant_id)
    await doc_repo.update(doc.id, file_path=file_path)

    # 4. 触发 Celery 异步解析（复用 P0 逻辑）
    try:
        from tasks.document_tasks import process_document

        process_document.delay(str(doc.id), tenant_id=str(tenant_id) if tenant_id else None)
        log.info("multipart 文档 %s 已触发 Celery 解析", doc.id)
    except ImportError:
        log.warning("Celery 未安装，文档 %s 需手动触发解析", doc.id)
    except Exception:
        log.exception("触发 Celery 解析失败，文档 %s 需手动处理", doc.id)

    # 5. 清理 Redis 会话（异步客户端，不阻塞事件循环）
    await _delete_multipart_session(upload_id)

    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )


@router.get("/documents/multipart/{upload_id}/parts", response_model=ApiResponse[dict])
async def list_uploaded_parts(
    upload_id: str,
    object_name: str = Query(..., description="对象存储路径"),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """查询已上传的分片列表 — 用于断点续传对账（P0/P1）。

    前端在中断恢复或 complete 前调用此端点，获取服务端实际已上传分片，
    与本地记录对比，找出缺失/冲突分片。

    Returns:
        {"parts": [{"part_number": 1, "etag": "...", "size": 10485760}, ...],
         "count": N}
    """
    # 安全：校验上传会话归属（防 IDOR — 阻止窥探他人上传会话的分片信息）
    _check_multipart_session(
        await _load_multipart_session(upload_id), user, object_name
    )

    try:
        from app.utils.minio_client import list_parts

        parts = await list_parts(
            bucket="ekb-documents",
            object_name=object_name,
            upload_id=upload_id,
        )
        return ApiResponse(
            code=0,
            data={"parts": parts, "count": len(parts)},
            message="success",
        )
    except ImportError:
        raise HTTPException(503, detail="MinIO 未安装")
    except Exception:
        log.exception("multipart.list_parts_failed")
        raise HTTPException(500, detail="查询已上传分片失败")


@router.delete("/documents/multipart/{upload_id}")
async def abort_multipart_upload(
    upload_id: str,
    object_name: str = Query(..., description="对象存储路径"),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """取消多段上传 — 清理已上传分片（P2-A）。

    用户取消上传时调用，MinIO 删除该 upload_id 下所有分片。
    幂等：重复调用或 upload_id 已失效时返回成功。

    安全：会话存在且属于其他用户时拒绝（防 IDOR — 阻止恶意取消
    他人正在进行的上传）；会话不存在时保持幂等语义返回成功。
    """
    # 安全：仅当会话存在且归属他人时拒绝；会话缺失保持幂等
    session = await _load_multipart_session(upload_id)
    if session is not None:
        _check_multipart_session(session, user, object_name)

    try:
        from app.utils.minio_client import abort_multipart_upload as _abort

        await _abort(
            bucket="ekb-documents",
            object_name=object_name,
            upload_id=upload_id,
        )
    except ImportError:
        raise HTTPException(503, detail="MinIO 未安装")
    except Exception:
        log.exception("multipart.abort_failed")
        # abort 失败不阻断用户操作，返回成功（幂等）

    # 清理 Redis 会话（异步客户端，不阻塞事件循环；幂等可重复调用）
    await _delete_multipart_session(upload_id)

    return ApiResponse(code=0, data={"aborted": True}, message="success")


@router.get("/documents/{doc_id}/progress", response_model=ApiResponse[dict])
async def get_document_parse_progress(
    doc_id: UUID,
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """查询文档解析进度（P1 增强）。

    返回 Celery 任务实时写入 Redis 的进度信息：
    - stage: queued / parsing / chunking / embedding / indexing / publishing / done / failed
    - current: 当前进度（如当前页码）
    - total: 总进度（如总页数、总分块数）
    - message: 人类可读提示

    前端可替代按轮询次数模拟的虚假进度，展示真实解析阶段。
    Redis 不可用或任务尚未启动时返回 stage="unknown"。
    """
    try:
        from tasks.document_tasks import get_parse_progress

        progress = get_parse_progress(str(doc_id))
        if progress is None:
            # 无进度记录 — 可能任务尚未启动或已超时清理
            progress = {"stage": "unknown", "message": "暂无进度信息"}
    except ImportError:
        log.debug("document_tasks 模块未安装，无法查询解析进度")
        progress = {"stage": "unknown", "message": "进度查询不可用"}
    except Exception:
        log.exception("查询文档 %s 解析进度失败", doc_id)
        progress = {"stage": "unknown", "message": "进度查询异常"}

    return ApiResponse(code=0, data=progress, message="success")


@router.get("/documents/{doc_id}", response_model=ApiResponse[DocResponse])
async def get_document(
    request: Request,
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """获取文档详情。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    doc = await service.get_document(doc_id)
    return ApiResponse(
        code=0,
        data=DocResponse.model_validate(doc),
        message="success",
    )


@router.put("/documents/{doc_id}", response_model=ApiResponse[DocResponse])
async def update_document(
    request: Request,
    doc_id: UUID,
    body: DocUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """更新文档内容（支持协同编辑场景）。

    更新前自动保存当前内容为版本快照（用于版本回溯）。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)

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
    request: Request,
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """软删除文档（仅所有者或 admin 可操作）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    doc = await service.get_document(doc_id)

    if doc.owner_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅所有者或管理员可删除文档",
        )

    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db)
    await doc_repo.soft_delete(doc_id)
    # P1: 文档删除后主动失效关联的 Token 缓存
    try:
        from app.rag.cache import TokenCache
        cache = TokenCache()
        await cache.invalidate_by_doc_id(str(doc_id))
    except Exception:
        pass
    return ApiResponse(code=0, message="success")


# ======================================================================
# 文档图片上传
# ======================================================================


@router.post(
    "/documents/{doc_id}/upload-image",
    response_model=ApiResponse[dict],
)
async def upload_document_image(
    request: Request,
    doc_id: UUID,
    file: UploadFile = File(..., description="图片文件"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """上传文档内图片到 MinIO，返回图片 URL。

    支持 PNG / JPG / GIF / WEBP 格式。

    P0: 文件大小校验 — 图片同样受 MAX_UPLOAD_SIZE_MB 限制。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    doc = await service.get_document(doc_id)

    # 校验文件类型
    allowed_types = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的图片类型: {file.content_type}",
        )

    # P0 安全修复（内存 DoS 防护）：分块流式读取 + 累计计数，超限即拒
    content_bytes = await _read_upload_bounded(file)
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
        from app.utils.minio_client import upload_file

        image_url = await upload_file(
            bucket="ekb-documents",
            object_name=object_name,
            data=content_bytes,
            content_type=file.content_type,
        )
    except ImportError:
        log.debug("MinIO 客户端未安装，返回占位 URL")
        image_url = f"/uploads/{object_name}"
    except Exception:
        log.exception("MinIO 图片上传失败")
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
    request: Request,
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[DocVersionResponse]]:
    """获取文档版本历史。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
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
    request: Request,
    doc_id: UUID,
    ver_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocResponse]:
    """恢复文档到指定历史版本。

    将历史版本的内容写回文档，并保存当前内容为新版本快照。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
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


# ======================================================================
# 文档解析摘要（P1 增强）
# ======================================================================


def _extract_structure_tags(text: str) -> list[str]:
    """从解析后的文本中提取结构标签列表。

    识别 HTML 标签（h1~h6/table/ul/li）和分页标记，
    返回去重后的结构标签列表（保持出现顺序）。

    Args:
        text: 解析后的文档文本（HTML 格式）。

    Returns:
        结构标签列表，如 ["h1", "h2", "table", "ul"]。
    """
    if not text:
        return []

    import re

    # 匹配 HTML 标签（h1~h6, table, ul, li）
    tags_seen: list[str] = []
    seen_set: set[str] = set()

    for match in re.finditer(r"<(h[1-6]|table|ul|li)\b", text, re.IGNORECASE):
        tag = match.group(1).lower()
        if tag not in seen_set:
            seen_set.add(tag)
            tags_seen.append(tag)

    return tags_seen


def _count_pages(text: str, doc_type: str) -> int:
    """推断文档页数/幻灯片数/工作表数。

    根据文档类型和解析标记推断：
    - PDF/DOCX: 统计分页标记 <!-- page: N --> 或 <h2> 数量
    - PPTX: 统计 <h2>幻灯片 数量
    - XLSX: 统计 <h2>sheet 标题数量
    - 其他: 0

    Args:
        text: 解析后的文档文本。
        doc_type: 文档类型。

    Returns:
        推断的页数，无法推断时返回 0。
    """
    if not text:
        return 0

    import re

    # 优先统计分页标记 <!-- page: N -->
    page_markers = re.findall(r"<!--\s*page:\s*\d+\s*-->", text)
    if page_markers:
        return len(page_markers)

    # 按文档类型统计 <h2> 标题
    h2_count = len(re.findall(r"<h2\b", text, re.IGNORECASE))

    if doc_type in ("pdf", "docx", "pptx", "xlsx"):
        return h2_count

    return 0


def _infer_parse_status(doc: Document) -> str:
    """推断文档的解析状态。

    根据文档状态和内容推断解析状态：
    - status=published/pending_review → "parsed"
    - status=draft 且有 content_text → "parsed"
    - status=draft 且无 content_text → "pending"
    - status=archived → "parsed"

    Args:
        doc: Document ORM 实例。

    Returns:
        解析状态字符串。
    """
    if doc.status in ("published", "pending_review", "archived"):
        return "parsed"
    if doc.status == "draft":
        return "parsed" if doc.content_text and doc.content_text.strip() else "pending"
    return "pending"


@router.get(
    "/documents/{doc_id}/summary",
    response_model=ApiResponse[DocumentSummaryResponse],
)
async def get_document_summary(
    request: Request,
    doc_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocumentSummaryResponse]:
    """获取文档解析摘要 — 对齐竞品草稿摘要 JSON。

    返回结构化摘要，包含：
    - preview: 正文前 500 字符预览
    - structure: 文档结构标签列表（h1/h2/table/ul 等）
    - warnings: 解析警告信息
    - pages: 推断的页数/幻灯片数/工作表数
    - char_count: 正文字符数
    - parse_status: 解析状态（parsed/partial/failed/pending）

    用于上传后立即展示解析结果概览，提升用户感知。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    doc = await service.get_document(doc_id)

    content_text = doc.content_text or ""
    content_html = doc.content_html or ""

    # 优先用 content_text 提取结构（解析器输出 HTML 格式）
    parse_output = content_text if content_text else content_html

    preview = content_text[:500] if content_text else ""
    structure = _extract_structure_tags(parse_output)

    # P1: 优先读 DB 持久化字段（解析时已计算），回退到动态计算（向后兼容历史数据）
    # page_count: DB 字段优先，NULL 或 0 时回退到动态计算
    db_page_count = getattr(doc, "page_count", None)
    if db_page_count and isinstance(db_page_count, int) and db_page_count > 0:
        pages = db_page_count
    else:
        pages = _count_pages(parse_output, doc.doc_type or "md")

    # char_count: DB 字段优先，NULL 或 0 时回退到动态计算
    db_char_count = getattr(doc, "char_count", None)
    if db_char_count and isinstance(db_char_count, int) and db_char_count > 0:
        char_count = db_char_count
    else:
        char_count = len(content_text)

    # parse_status: DB 字段优先，NULL 时回退到推断
    db_parse_status = getattr(doc, "parse_status", None)
    if db_parse_status and isinstance(db_parse_status, str):
        parse_status = db_parse_status
    else:
        parse_status = _infer_parse_status(doc)

    # parse_warnings: DB 字段优先，NULL 时回退到动态推断
    db_parse_warnings = getattr(doc, "parse_warnings", None)
    if db_parse_warnings and isinstance(db_parse_warnings, list):
        warnings = list(db_parse_warnings)
    else:
        # 警告信息 — 基于文档状态和内容推断（历史数据兼容）
        warnings = []
        if not content_text.strip():
            warnings.append("文档正文为空，可能解析失败或尚未完成解析")
        if doc.doc_type in ("doc", "ppt"):
            warnings.append(f"旧格式 .{doc.doc_type} 不支持解析，请转换为 .{doc.doc_type}x 后重新上传")
        if doc.status == "draft" and not content_text:
            warnings.append("文档处于草稿状态，等待异步解析任务完成")

    summary = DocumentSummaryResponse(
        doc_id=doc.id,
        title=doc.title,
        doc_type=doc.doc_type or "md",
        status=doc.status,
        preview=preview,
        structure=structure,
        warnings=warnings,
        pages=pages,
        char_count=char_count,
        parse_status=parse_status,
        file_path=doc.file_path,
        created_at=doc.created_at,
    )

    return ApiResponse(code=0, data=summary, message="success")


# ======================================================================
# P0 多平台文档导入 — Confluence / Obsidian / 飞书 / Notion
# ======================================================================


@router.post(
    "/documents/import",
    response_model=ApiResponse[DocumentImportResponse],
    status_code=201,
)
async def import_document_from_source(
    request: Request,
    req: DocumentImportRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocumentImportResponse]:
    """从外部平台导入文档 — 通过适配器拉取后创建 Document 并触发异步解析。

    流程：
        1. 从 adapter_registry 获取对应平台适配器
        2. 调用 adapter.fetch() 拉取文档（HTML 或 Markdown）
        3. 创建 Document 记录（content_text 存原始内容，doc_type 按格式映射）
        4. 触发 Celery 异步解析任务（HTML 清洗 / Markdown 解析 → chunker → 向量化）

    支持平台：
        - confluence: Confluence REST API → HTML（WikiHtmlCleaner 清洗）
        - obsidian: 本地 .md 文件 → Markdown（MarkdownParser 解析）
        - feishu: 飞书 OpenAPI 导出 → DOCX（DOCXParser 解析）
        - notion: Notion blocks API → Markdown（MarkdownParser 解析）

    凭证通过 credentials dict 传入，不持久化到数据库。
    """
    from app.document.source_adapters.base import AdapterError
    from app.document.source_adapters.registry import adapter_registry

    # 1. 获取适配器
    adapter = adapter_registry.get(req.source)
    if adapter is None:
        available = [a["adapter_id"] for a in adapter_registry.list_adapters()]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"不支持的平台: {req.source}。"
                f"已注册平台: {', '.join(available) or '无'}"
            ),
        )

    # 2. 拉取文档
    try:
        fetched = await adapter.fetch(req.doc_url_or_id, req.credentials)
    except AdapterError as exc:
        log.warning(
            "document.import.fetch_failed",
            source=req.source,
            doc_url_or_id=req.doc_url_or_id[:100],
            error=str(exc),
            status_code=exc.status_code,
        )
        http_status = (
            status.HTTP_404_NOT_FOUND
            if exc.status_code == 404
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=http_status,
            detail=f"拉取文档失败: {exc}",
        ) from exc
    except Exception as exc:
        log.exception(
            "document.import.unexpected_error",
            source=req.source,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"拉取文档时发生意外错误: {exc}",
        ) from exc

    if not fetched.content or not fetched.content.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="拉取到的文档内容为空",
        )

    # 3. 确定标题和文档类型
    doc_title = req.title or fetched.title or f"Imported from {req.source}"
    # 格式映射：html → html，markdown → md
    doc_type = "html" if fetched.format == "html" else "md"

    # 4. 创建文档记录
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, user, tenant_id=tenant_id)
    try:
        doc = await service.upload_document(
            kb_id=req.kb_id,
            title=doc_title,
            content=fetched.content,
            doc_type=doc_type,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    # 5. 更新 file_path 和 classification
    from app.repositories.knowledge_repository import DocumentRepository

    doc_repo = DocumentRepository(db)
    await doc_repo.update(
        doc.id,
        file_path=fetched.source_url or f"{req.source}://{fetched.doc_id}",
        classification=req.classification.value,
    )

    # 6. 触发 Celery 异步解析任务
    try:
        from tasks.document_tasks import process_document

        process_document.delay(str(doc.id), tenant_id=str(tenant_id) if tenant_id else None)
        log.info(
            "document.import.triggered_parse",
            doc_id=str(doc.id),
            source=req.source,
        )
    except ImportError:
        log.warning(
            "document.import.celery_unavailable",
            doc_id=str(doc.id),
        )
    except Exception:
        log.exception(
            "document.import.celery_error",
            doc_id=str(doc.id),
        )

    log.info(
        "document.import.success",
        doc_id=str(doc.id),
        source=req.source,
        title=doc_title,
        format=fetched.format,
        chars=len(fetched.content),
    )

    return ApiResponse(
        code=0,
        data=DocumentImportResponse(
            doc_id=doc.id,
            source=req.source,
            title=doc_title,
            source_url=fetched.source_url,
            format=fetched.format,
            status="draft",
            message="导入成功，正在异步解析",
        ),
        message="success",
    )
