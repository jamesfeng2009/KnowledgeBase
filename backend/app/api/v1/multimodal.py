"""
多模态处理 API — 单一职责：提供图片/表格/扫描件/白板处理的 HTTP 端点。

端点：
    POST /multimodal/image      — 图片智能解析
    POST /multimodal/table       — 表格结构化
    POST /multimodal/scanned-pdf — 扫描件 OCR
    POST /multimodal/whiteboard  — 白板拍照入库
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.models.user import User
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/multimodal", tags=["multimodal"])


@router.post("/image")
async def process_image(
    request: Request,
    file: UploadFile = File(..., description="图片文件"),
    user: User = Depends(require_module("multimodal")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """图片智能解析 — VLM 生成图片描述和标签。

    返回描述文本和关键词标签，用于文档索引增强。
    """
    from app.services.multimodal_service import MultimodalService

    image_data = await file.read()
    mime_type = file.content_type or "image/png"

    tenant_id = getattr(request.state, "tenant_id", None)
    service = MultimodalService(tenant_id=tenant_id)
    result = await service.process_image(image_data, mime_type)
    return ApiResponse(code=0, data=result, message="success")


@router.post("/table")
async def process_table(
    request: Request,
    file: UploadFile = File(..., description="表格图片文件"),
    user: User = Depends(require_module("multimodal")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """表格结构化 — VLM 识别表格行列结构，返回 JSON。

    用于政策费率表、产品参数表等结构化数据。
    """
    from app.services.multimodal_service import MultimodalService

    image_data = await file.read()
    mime_type = file.content_type or "image/png"

    tenant_id = getattr(request.state, "tenant_id", None)
    service = MultimodalService(tenant_id=tenant_id)
    result = await service.process_table(image_data, mime_type)
    return ApiResponse(code=0, data=result, message="success")


@router.post("/scanned-pdf")
async def process_scanned_pdf(
    request: Request,
    file: UploadFile = File(..., description="扫描件图片"),
    user: User = Depends(require_module("multimodal")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """扫描件 OCR — VLM 识别扫描件文字。

    返回纯文本，用于合同、发票等扫描件入库。
    """
    from app.services.multimodal_service import MultimodalService

    image_data = await file.read()
    mime_type = file.content_type or "image/png"

    tenant_id = getattr(request.state, "tenant_id", None)
    service = MultimodalService(tenant_id=tenant_id)
    result = await service.process_scanned_pdf(image_data, mime_type)
    return ApiResponse(code=0, data={"text": result}, message="success")


@router.post("/whiteboard")
async def process_whiteboard(
    request: Request,
    file: UploadFile = File(..., description="白板照片"),
    user: User = Depends(require_module("multimodal")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """白板拍照入库 — VLM 理解白板内容，生成会议纪要。

    返回摘要、要点和行动项。
    """
    from app.services.multimodal_service import MultimodalService

    image_data = await file.read()
    mime_type = file.content_type or "image/png"

    tenant_id = getattr(request.state, "tenant_id", None)
    service = MultimodalService(tenant_id=tenant_id)
    result = await service.process_whiteboard(image_data, mime_type)
    return ApiResponse(code=0, data=result, message="success")
