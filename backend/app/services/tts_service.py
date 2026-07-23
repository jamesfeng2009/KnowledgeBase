"""TTS 语音合成服务 — 基于 edge-tts（Microsoft Edge 在线 TTS，免费无 API Key）。

P1: 为 AI 对话回复提供语音输出能力。

设计要点：
    - 使用 edge-tts 异步合成 MP3 音频，无需 GPU/API Key
    - 支持中文/英文/多语种语音（通过 voice 参数选择）
    - 合成结果为 MP3 字节流，前端用 HTML5 Audio 播放
    - TTS_ENABLED=False 或 edge-tts 未安装时静默降级

使用示例::

    from app.services.tts_service import synthesize_text

    audio_bytes = await synthesize_text("你好世界", voice="zh-CN-XiaoxiaoNeural")
    # audio_bytes 为 MP3 格式字节流
"""

from __future__ import annotations

import io
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

# 支持的中文语音列表（常用子集，完整列表见 edge-tts --list-voices）
SUPPORTED_VOICES: dict[str, str] = {
    "zh-CN-XiaoxiaoNeural": "中文（简体）- 晓晓（女声）",
    "zh-CN-YunxiNeural": "中文（简体）- 云希（男声）",
    "zh-CN-XiaoyiNeural": "中文（简体）- 晓伊（女声）",
    "zh-CN-YunyangNeural": "中文（简体）- 云扬（男声）",
    "en-US-AriaNeural": "English (US) - Aria (Female)",
    "en-US-GuyNeural": "English (US) - Guy (Male)",
}


async def synthesize_text(
    text: str,
    voice: str | None = None,
    rate: str | None = None,
    volume: str | None = None,
) -> bytes:
    """将文本合成为 MP3 音频字节流。

    Args:
        text: 要合成的文本（支持中英文混排）。
        voice: 语音名称（如 "zh-CN-XiaoxiaoNeural"），为 None 时使用配置默认值。
        rate: 语速（如 "+0%", "-20%", "+50%"），为 None 时使用配置默认值。
        volume: 音量（如 "+0%", "-10%"），为 None 时使用配置默认值。

    Returns:
        MP3 格式的音频字节流。

    Raises:
        RuntimeError: TTS 未启用或 edge-tts 未安装。
        ValueError: 文本为空或过长。
    """
    if not settings.TTS_ENABLED:
        raise RuntimeError("TTS 功能未启用（TTS_ENABLED=False）")

    if not text or not text.strip():
        raise ValueError("TTS 合成文本不能为空")

    # 限制文本长度，防止滥用（约 5000 字 ≈ 10 分钟音频）
    if len(text) > 5000:
        log.warning("tts.text_truncated", original_len=len(text), max_len=5000)
        text = text[:5000]

    voice = voice or settings.TTS_VOICE
    rate = rate or settings.TTS_RATE
    volume = volume or settings.TTS_VOLUME

    try:
        import edge_tts
    except ImportError:
        log.error("tts.edge_tts_not_installed")
        raise RuntimeError("edge-tts 未安装，请运行 pip install edge-tts")

    log.info(
        "tts.synthesize.start",
        text_len=len(text),
        voice=voice,
        rate=rate,
    )

    # edge-tts 的 communicate 方法生成 MP3 音频流
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        volume=volume,
    )

    # 收集音频流为字节
    audio_buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_buffer.write(chunk["data"])

    audio_bytes = audio_buffer.getvalue()
    log.info(
        "tts.synthesize.done",
        audio_size=len(audio_bytes),
        voice=voice,
    )
    return audio_bytes


def get_available_voices() -> list[dict[str, str]]:
    """获取可用的语音列表。

    Returns:
        语音信息列表，每项含 voice 和 description。
    """
    return [
        {"voice": v, "description": desc}
        for v, desc in SUPPORTED_VOICES.items()
    ]
