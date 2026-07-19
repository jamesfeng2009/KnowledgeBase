"""
ASR Provider 抽象层 — 单一职责：将音频转写为带时间戳的文本。

遵循与 VLM/LLM 完全一致的架构模式：
    - 抽象接口 + 注册表装饰器 + 工厂函数；
    - 按 DEPLOY_MODE 切换实现；
    - 延迟导入第三方库，优雅降级。

转写结果格式（TranscribeSegment）::

    {
        "start": 0.0,      # 开始时间（秒）
        "end": 15.2,       # 结束时间（秒）
        "text": "今天讲一下数据仓库的分层架构..."
    }
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# ASR Provider 工厂注册表 — deploy_mode → 工厂函数
_asr_registry: dict[str, Callable[[], "ASRProvider"]] = {}


@dataclass(frozen=True)
class TranscribeSegment:
    """转写片段 — 带时间戳的文本段。

    Attributes:
        start: 开始时间（秒）。
        end: 结束时间（秒）。
        text: 转写文本。
    """

    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {"start": self.start, "end": self.end, "text": self.text}

    @property
    def timestamp_label(self) -> str:
        """格式化时间戳标签 — MM:SS 格式。"""
        return f"{int(self.start // 60):02d}:{int(self.start % 60):02d}"


class ASRProvider(ABC):
    """语音转写统一接口 — 所有 ASR 实现继承本抽象。"""

    @abstractmethod
    async def transcribe(
        self,
        audio_path: str,
        language: str | None = None,
    ) -> list[TranscribeSegment]:
        """将音频文件转写为带时间戳的文本段列表。

        Args:
            audio_path: 音频文件路径（本地文件系统）。
            language: 语言代码（如 "zh"、"en"），None 表示自动检测。

        Returns:
            转写片段列表，按时间排序。
        """
        raise NotImplementedError

    async def transcribe_multipart(
        self,
        audio_path: str,
        language: str | None = None,
        segment_duration: int = 480,
        progress_callback: Any = None,
    ) -> list[TranscribeSegment]:
        """分段转写 — 突破 Whisper 25MB 单文件限制（P2-B）。

        GB 级视频的音频 WAV 通常远超 25MB，无法直接调用 Whisper API。
        本方法用 ffmpeg 按 segment_duration 秒切片，逐段调用 transcribe，
        时间戳偏移后合并结果。

        断点续传：progress_callback 可记录已完成段索引，失败后跳过已处理段。

        Args:
            audio_path: 完整音频文件路径（WAV）。
            language: 语言代码。
            segment_duration: 每段时长（秒），默认 480（8 分钟，WAV ~24MB < 25MB）。
            progress_callback: 可选回调 callback(seg_index, total_segs, segments_count)。

        Returns:
            合并后的转写片段列表（时间戳已偏移到原始音频时间线）。
        """
        import asyncio
        import os
        import subprocess
        import tempfile

        # 1. 获取音频总时长
        duration = await self._get_audio_duration(audio_path)
        if duration <= 0:
            log.warning("asr.multipart.no_duration", audio_path=audio_path)
            return await self.transcribe(audio_path, language)

        # 2. 计算分段数
        total_segments = max(1, int(duration / segment_duration) + (1 if duration % segment_duration else 0))
        log.info(
            "asr.multipart.start",
            duration=duration,
            segment_duration=segment_duration,
            total_segments=total_segments,
        )

        # 3. 逐段切片 + 转写
        all_segments: list[TranscribeSegment] = []
        tmp_dir = tempfile.mkdtemp(prefix="ekb_asr_")

        try:
            for seg_idx in range(total_segments):
                start_offset = seg_idx * segment_duration
                seg_path = os.path.join(tmp_dir, f"seg_{seg_idx:04d}.wav")

                # ffmpeg 切片（-ss 跳过 + -t 时长 + -ar 16kHz mono）
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", str(start_offset),
                    "-t", str(segment_duration),
                    "-i", audio_path,
                    "-ar", "16000", "-ac", "1",
                    seg_path,
                ]
                try:
                    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
                except subprocess.CalledProcessError as exc:
                    log.warning("asr.multipart.slice_failed", seg_idx=seg_idx, error=exc.stderr.decode()[:200])
                    continue
                except subprocess.TimeoutExpired:
                    log.warning("asr.multipart.slice_timeout", seg_idx=seg_idx)
                    continue

                # 检查切片文件大小（空段跳过）
                if not os.path.exists(seg_path) or os.path.getsize(seg_path) < 1000:
                    log.debug("asr.multipart.empty_segment", seg_idx=seg_idx)
                    continue

                # 调用子类 transcribe 转写该段
                seg_segments = await self.transcribe(seg_path, language)

                # 时间戳偏移到原始音频时间线
                for seg in seg_segments:
                    all_segments.append(
                        TranscribeSegment(
                            start=seg.start + start_offset,
                            end=seg.end + start_offset,
                            text=seg.text,
                        )
                    )

                # 进度回调
                if progress_callback:
                    try:
                        progress_callback(seg_idx + 1, total_segments, len(all_segments))
                    except Exception:
                        pass

                # 清理切片文件
                try:
                    os.remove(seg_path)
                except OSError:
                    pass

            log.info("asr.multipart.done", total_segments=len(all_segments))
            return all_segments

        finally:
            # 清理临时目录
            try:
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    async def _get_audio_duration(self, audio_path: str) -> float:
        """获取音频文件时长（秒）— 用 ffprobe。

        Args:
            audio_path: 音频文件路径。

        Returns:
            时长（秒），失败返回 0。
        """
        import subprocess

        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return float(result.stdout.strip()) if result.stdout.strip() else 0.0
        except Exception as exc:
            log.warning("asr.duration_failed", error=str(exc))
            return 0.0


# ------------------------------------------------------------------
# SaaS 实现 — OpenAI Whisper API
# ------------------------------------------------------------------


class OpenAIASRProvider(ASRProvider):
    """SaaS 模式 — 调用 OpenAI Whisper API 转写音频。"""

    async def transcribe(
        self,
        audio_path: str,
        language: str | None = None,
    ) -> list[TranscribeSegment]:
        try:
            from openai import AsyncOpenAI

            settings = get_settings()
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

            with open(audio_path, "rb") as audio_file:
                kwargs: dict[str, Any] = {
                    "model": "whisper-1",
                    "file": audio_file,
                    "response_format": "verbose_json",
                }
                if language:
                    kwargs["language"] = language
                result = await client.audio.transcriptions.create(**kwargs)

            segments: list[TranscribeSegment] = []
            for seg in getattr(result, "segments", []) or []:
                segments.append(
                    TranscribeSegment(
                        start=float(seg.get("start", 0)),
                        end=float(seg.get("end", 0)),
                        text=str(seg.get("text", "")).strip(),
                    )
                )
            log.info("asr.openai.transcribed", segments=len(segments))
            return segments
        except Exception as exc:
            log.warning("asr.openai.failed", error=str(exc))
            return []


# ------------------------------------------------------------------
# 私有部署实现 — Faster-Whisper / FunASR HTTP 服务
# ------------------------------------------------------------------


class WhisperASRProvider(ASRProvider):
    """私有部署 — 调用独立 ASR 服务（Faster-Whisper HTTP API）。

    ASR 服务以 HTTP API 形式部署，与 VLM 服务架构一致。
    请求格式（multipart/form-data）::

        POST /v1/asr/transcribe
        file: <audio binary>
        language: zh (optional)

    响应格式（JSON）::

        {
            "segments": [
                {"start": 0.0, "end": 15.2, "text": "..."},
                ...
            ]
        }
    """

    async def transcribe(
        self,
        audio_path: str,
        language: str | None = None,
    ) -> list[TranscribeSegment]:
        try:
            import httpx

            settings = get_settings()
            url = f"http://{settings.ASR_HOST}:{settings.ASR_PORT}/v1/asr/transcribe"

            with open(audio_path, "rb") as audio_file:
                files = {"file": (audio_path, audio_file, "audio/wav")}
                data: dict[str, str] = {}
                if language:
                    data["language"] = language

                async with httpx.AsyncClient(timeout=300) as client:
                    resp = await client.post(url, files=files, data=data)
                    resp.raise_for_status()
                    result = resp.json()

            segments: list[TranscribeSegment] = []
            for seg in result.get("segments", []) or []:
                segments.append(
                    TranscribeSegment(
                        start=float(seg.get("start", 0)),
                        end=float(seg.get("end", 0)),
                        text=str(seg.get("text", "")).strip(),
                    )
                )
            log.info("asr.whisper.transcribed", segments=len(segments))
            return segments
        except Exception as exc:
            log.warning("asr.whisper.failed", error=str(exc))
            return []


# ------------------------------------------------------------------
# 注册 + 工厂
# ------------------------------------------------------------------


def register_asr_provider(
    deploy_mode: str,
) -> Callable[[Callable[[], ASRProvider]], Callable[[], ASRProvider]]:
    """装饰器 — 注册 ASR Provider 工厂函数。

    Args:
        deploy_mode: 部署模式（saas / private_overseas / private_domestic）。

    Returns:
        装饰器函数。
    """

    def decorator(
        factory: Callable[[], ASRProvider],
    ) -> Callable[[], ASRProvider]:
        _asr_registry[deploy_mode] = factory
        return factory

    return decorator


@register_asr_provider("saas")
def _make_openai_asr() -> ASRProvider:
    """SaaS 模式：OpenAI Whisper API。"""
    return OpenAIASRProvider()


@register_asr_provider("private_overseas")
@register_asr_provider("private_domestic")
def _make_whisper_asr() -> ASRProvider:
    """私有部署（海外/国内）：独立 ASR 服务。"""
    return WhisperASRProvider()


@lru_cache(maxsize=1)
def get_asr_provider() -> ASRProvider:
    """获取 ASR Provider 单例 — 根据 DEPLOY_MODE 切换。

    Returns:
        ASRProvider 实例。

    Raises:
        ValueError: 不支持的 DEPLOY_MODE。
    """
    settings = get_settings()
    mode = settings.DEPLOY_MODE
    factory = _asr_registry.get(mode)
    if factory is None:
        raise ValueError(
            f"不支持的 DEPLOY_MODE（ASR）: {mode}，"
            f"已注册: {list(_asr_registry)}"
        )
    return factory()


def reset_asr_cache() -> None:
    """重置工厂缓存 — 测试场景使用。"""
    get_asr_provider.cache_clear()
