"""
ASR 语音转写层 — 对外暴露统一的音频转写接口。

双模式实现（与 VLM/LLM 同模式）：
    - OpenAIASRProvider：SaaS 模式，调用 OpenAI Whisper API；
    - WhisperASRProvider：私有部署，调用独立 ASR 服务（Faster-Whisper / FunASR）；
    - get_asr_provider()：工厂函数，根据 DEPLOY_MODE 切换。

遵循开闭原则：新增 ASRProvider 只需继承并通过 register_asr_provider 注册。
遵循优雅降级：ASR 服务不可用时返回空转写结果而非抛异常。
"""

from __future__ import annotations

from app.asr.provider import (
    ASRProvider,
    TranscribeSegment,
    get_asr_provider,
    register_asr_provider,
)

__all__ = [
    "ASRProvider",
    "TranscribeSegment",
    "get_asr_provider",
    "register_asr_provider",
]
