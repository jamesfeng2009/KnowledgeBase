"""
视频处理器 — 单一职责：提取音轨 + 抽取关键帧。

通过 ffmpeg 命令行工具完成：
    - extract_audio(): 提取音轨为 16kHz mono WAV（ASR 标准输入格式）；
    - extract_keyframes(): 按场景变化检测抽取关键帧（PNG）。

遵循优雅降级：ffmpeg 未安装时返回空结果并记录日志，不阻断主流程。
遵循开闭原则：新增视频处理能力只需扩展 VideoProcessor 方法。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger
from functools import lru_cache

log = get_logger(__name__)


@dataclass(frozen=True)
class KeyFrame:
    """关键帧 — 带时间戳的图片路径。

    Attributes:
        timestamp: 帧时间戳（秒）。
        image_path: 帧图片文件路径。
    """

    timestamp: float
    image_path: str

    @property
    def timestamp_label(self) -> str:
        """格式化时间戳标签 — MM:SS 格式。"""
        return f"{int(self.timestamp // 60):02d}:{int(self.timestamp % 60):02d}"


class VideoProcessor:
    """视频处理器 — 通过 ffmpeg 提取音轨和关键帧。

    ffmpeg 命令说明：
        - 音轨提取：-vn -acodec pcm_s16le -ar 16000 -ac 1
          （丢弃视频，PCM 16-bit，16kHz，单声道 — ASR 标准格式）
        - 关键帧抽取：-vf "select='gt(scene,0.3)'" -vsync vfr
          （场景变化 > 0.3 时抽取帧，variable frame rate）
    """

    # 场景变化阈值 — 0.3 适合 PPT/培训视频，值越低抽帧越密集
    SCENE_THRESHOLD: float = 0.3
    # 最大关键帧数 — 防止超长视频抽出过多帧
    MAX_KEYFRAMES: int = 100
    # 关键帧抽取间隔（秒）— 兜底：场景检测不够时按固定间隔补抽
    FALLBACK_INTERVAL: int = 30

    async def extract_audio(self, video_path: str) -> str:
        """从视频提取音轨为 WAV 文件。

        Args:
            video_path: 视频文件路径。

        Returns:
            WAV 音频文件路径。失败时返回空字符串。
        """
        if not video_path or not os.path.exists(video_path):
            log.warning("video.audio.no_file", path=video_path)
            return ""

        wav_path = tempfile.mktemp(suffix=".wav", prefix="asr_")

        cmd = [
            "ffmpeg", "-i", video_path,
            "-vn",                          # 丢弃视频
            "-acodec", "pcm_s16le",         # PCM 16-bit
            "-ar", "16000",                 # 16kHz
            "-ac", "1",                     # 单声道
            "-y",                           # 覆盖输出
            wav_path,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                log.warning(
                    "video.audio.ffmpeg_error",
                    returncode=process.returncode,
                    stderr=stderr.decode()[:500] if stderr else "",
                )
                return ""
            log.info("video.audio.extracted", wav=wav_path)
            return wav_path
        except FileNotFoundError:
            log.warning("video.audio.ffmpeg_not_installed")
            return ""
        except Exception as exc:
            log.warning("video.audio.extract_failed", error=str(exc))
            return ""

    async def extract_keyframes(self, video_path: str) -> list[KeyFrame]:
        """从视频抽取关键帧。

        使用场景变化检测自动抽取画面变化的帧（适合 PPT/培训视频）。
        如果场景检测抽出的帧数不足，按固定间隔补充。

        Args:
            video_path: 视频文件路径。

        Returns:
            关键帧列表。失败时返回空列表。
        """
        if not video_path or not os.path.exists(video_path):
            log.warning("video.keyframes.no_file", path=video_path)
            return []

        settings = get_settings()
        if not settings.VIDEO_KEYFRAME_ENABLED:
            log.info("video.keyframes.disabled")
            return []

        output_dir = tempfile.mkdtemp(prefix="keyframes_")
        output_pattern = os.path.join(output_dir, "frame_%04d.png")

        # 场景变化检测抽帧
        threshold = self.SCENE_THRESHOLD
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-vsync", "vfr",
            "-frame_pts", "1",               # 用 PTS 作为文件名时间戳
            "-y",
            output_pattern,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except FileNotFoundError:
            log.warning("video.keyframes.ffmpeg_not_installed")
            return []
        except Exception as exc:
            log.warning("video.keyframes.extract_failed", error=str(exc))
            return []

        # 解析输出的帧文件
        keyframes = self._parse_frame_files(output_dir, video_path)

        # 如果场景检测抽出的帧太少，按固定间隔补抽
        if len(keyframes) < 5:
            keyframes = await self._fallback_extract(
                video_path, output_dir, keyframes
            )

        # 限制最大帧数
        if len(keyframes) > self.MAX_KEYFRAMES:
            # 均匀采样
            step = len(keyframes) / self.MAX_KEYFRAMES
            keyframes = [keyframes[int(i * step)] for i in range(self.MAX_KEYFRAMES)]

        log.info("video.keyframes.extracted", count=len(keyframes))
        return keyframes

    def _parse_frame_files(
        self,
        output_dir: str,
        video_path: str,
    ) -> list[KeyFrame]:
        """解析输出目录中的帧文件为 KeyFrame 列表。"""
        keyframes: list[KeyFrame] = []
        if not os.path.isdir(output_dir):
            return keyframes

        for filename in sorted(os.listdir(output_dir)):
            if not filename.endswith(".png"):
                continue
            filepath = os.path.join(output_dir, filename)
            # 从文件名解析时间戳 — frame_pts 模式下文件名是 PTS
            # 回退：按文件序号估算
            timestamp = self._estimate_timestamp_from_filename(filename)
            keyframes.append(
                KeyFrame(timestamp=timestamp, image_path=filepath)
            )

        return keyframes

    def _estimate_timestamp_from_filename(self, filename: str) -> float:
        """从帧文件名估算时间戳。"""
        try:
            # frame_XXXX.png — XXXX 是序号
            parts = filename.replace(".png", "").split("_")
            if len(parts) >= 2:
                idx = int(parts[-1])
                # 按 30 秒间隔估算（兜底策略）
                return float(idx * self.FALLBACK_INTERVAL)
        except (ValueError, IndexError):
            pass
        return 0.0

    async def _fallback_extract(
        self,
        video_path: str,
        output_dir: str,
        existing: list[KeyFrame],
    ) -> list[KeyFrame]:
        """按固定间隔补抽关键帧（场景检测不够时）。"""
        # 先获取视频时长
        duration = await self._get_duration(video_path)
        if duration <= 0:
            return existing

        interval = self.FALLBACK_INTERVAL
        existing_set = {kf.timestamp for kf in existing}

        for t in range(0, int(duration), interval):
            if any(abs(t - et) < 5 for et in existing_set):
                continue  # 已有附近的帧

            frame_path = os.path.join(
                output_dir, f"fallback_{t:04d}.png"
            )
            cmd = [
                "ffmpeg", "-i", video_path,
                "-ss", str(t),
                "-frames:v", "1",
                "-y",
                frame_path,
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await process.communicate()
                if os.path.exists(frame_path):
                    existing.append(
                        KeyFrame(timestamp=float(t), image_path=frame_path)
                    )
            except Exception as exc:
                log.warning("video.keyframes.fallback_failed", t=t, error=str(exc))

        existing.sort(key=lambda kf: kf.timestamp)
        return existing

    async def _get_duration(self, video_path: str) -> float:
        """获取视频时长（秒）。"""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            return float(stdout.decode().strip())
        except Exception:
            return 0.0


@lru_cache(maxsize=1)
def get_video_processor() -> VideoProcessor:
    """获取 VideoProcessor 单例。"""
    return VideoProcessor()
