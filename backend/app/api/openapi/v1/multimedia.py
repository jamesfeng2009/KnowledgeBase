"""多媒体开放 API — 文档解析、图片理解与音频转文字。

为外部系统提供非结构化数据的结构化处理能力：
- 文档解析：上传文件，返回结构化文本；
- 图片理解（VLM）：基于视觉语言模型分析图片内容；
- 音频转文字：语音转文本（预留接口）。

权限说明：
- 需要 scope: ``multimedia:parse`` / ``multimedia:vision``；
- 认证方式为 API Key（X-API-Key header）。
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, UploadFile, File
from pydantic import BaseModel, Field

from app.api.openapi.deps import require_scope
from app.config import get_settings
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/multimedia", tags=["开放接口-多媒体"])

settings = get_settings()


# ======================================================================
# 请求 Schema
# ======================================================================


class VisionRequest(BaseModel):
    """图片理解请求。"""

    image_base64: str = Field(..., description="Base64 编码的图片数据")
    prompt: str = Field(
        default="请描述这张图片的内容。",
        description="针对图片的提问/指令",
    )


class ParseResponse(BaseModel):
    """文档解析结果。"""

    filename: str = Field(..., description="文件名")
    doc_type: str = Field(..., description="文档类型")
    text: str = Field(..., description="解析出的纯文本")
    pages: int = Field(default=0, description="页数")


# ======================================================================
# 端点
# ======================================================================


@router.post("/parse", response_model=ApiResponse[dict])
async def parse_document(
    file: UploadFile = File(..., description="待解析的文档文件"),
    api_key_info: dict = Depends(require_scope("multimedia:parse")),
) -> ApiResponse[dict]:
    """文档解析 — 上传文件，返回结构化文本。

    支持的文件类型取决于底层解析引擎（LlamaIndex readers）。
    当前为预留接口，返回文件元信息供联调。
    """
    filename = file.filename or "unknown"
    content = await file.read()

    logger.info(
        "openapi.multimedia.parse",
        filename=filename,
        size=len(content),
        key_name=api_key_info.get("name"),
    )

    # 预留接口：实际解析逻辑由 LlamaIndex readers 实现
    doc_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    return ApiResponse(
        code=0,
        data={
            "filename": filename,
            "doc_type": doc_type,
            "text": "",
            "pages": 0,
            "message": "文档解析为预留接口，请对接解析引擎后替换",
        },
        message="success",
    )


@router.post("/vision", response_model=ApiResponse[dict])
async def vision(
    body: VisionRequest,
    api_key_info: dict = Depends(require_scope("multimedia:vision")),
) -> ApiResponse[dict]:
    """图片理解（VLM）— 基于视觉语言模型分析图片内容。

    底层 VLM 由 DEPLOY_MODE 决定（私有部署使用 Pixtral-12B）。
    当前为预留接口，返回占位结构供联调。
    """
    logger.info(
        "openapi.multimedia.vision",
        prompt=body.prompt[:50],
        image_size=len(body.image_base64),
        key_name=api_key_info.get("name"),
    )

    return ApiResponse(
        code=0,
        data={
            "description": "",
            "model": settings.VLM_MODEL,
            "message": "VLM 图片理解为预留接口，请对接视觉模型后替换",
        },
        message="success",
    )


@router.post("/transcribe", response_model=ApiResponse[dict])
async def transcribe(
    file: UploadFile = File(..., description="待转写的音频文件"),
    api_key_info: dict = Depends(require_scope("multimedia:parse")),
) -> ApiResponse[dict]:
    """音频转文字 — 语音转文本（预留接口）。

    预留接口，后续对接 ASR 引擎后实现。
    """
    filename = file.filename or "unknown"
    content = await file.read()

    logger.info(
        "openapi.multimedia.transcribe",
        filename=filename,
        size=len(content),
        key_name=api_key_info.get("name"),
    )

    return ApiResponse(
        code=0,
        data={
            "filename": filename,
            "text": "",
            "message": "音频转文字为预留接口，请对接 ASR 引擎后替换",
        },
        message="success",
    )
