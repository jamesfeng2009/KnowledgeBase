"""TTS 语音合成 API — 将文本转为 MP3 音频。

P1: 为 AI 对话回复提供语音输出能力。

端点：
    POST /tts/synthesize  — 文本 → MP3 音频流
    GET  /tts/voices      — 获取可用语音列表
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services.tts_service import get_available_voices, synthesize_text
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["TTS 语音合成"])


class TTSSynthesizeRequest(BaseModel):
    """TTS 合成请求。"""

    text: str
    voice: str | None = None
    rate: str | None = None
    volume: str | None = None

    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("文本不能为空")
        if len(v) > 5000:
            raise ValueError("文本长度不能超过 5000 字符")
        return v


class VoiceItem(BaseModel):
    """语音信息。"""

    voice: str
    description: str


@router.post("/tts/synthesize")
async def tts_synthesize(
    request: TTSSynthesizeRequest,
    user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """将文本合成为 MP3 音频流。

    权限：所有认证用户。
    返回：audio/mpeg 流（MP3 格式）。

    使用方式：前端用 ``fetch`` 请求此端点，将返回的 blob 用
    ``URL.createObjectURL`` + ``Audio`` 播放。
    """
    log.info(
        "api.tts.synthesize",
        user_id=str(user.id),
        text_len=len(request.text),
        voice=request.voice,
    )

    audio_bytes = await synthesize_text(
        text=request.text,
        voice=request.voice,
        rate=request.rate,
        volume=request.volume,
    )

    return StreamingResponse(
        iter([audio_bytes]),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": "inline; filename=tts_output.mp3",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/tts/voices", response_model=ApiResponse[list[VoiceItem]])
async def get_tts_voices(
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[VoiceItem]]:
    """获取可用的 TTS 语音列表。

    权限：所有认证用户。
    """
    voices = get_available_voices()
    return ApiResponse(
        code=0,
        data=[VoiceItem(**v) for v in voices],
    )
