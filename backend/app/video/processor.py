"""
视频处理器 — 单一职责：提取音轨 + 抽取关键帧。

通过 ffmpeg 命令行工具完成：
    - extract_audio(): 提取音轨为 16kHz mono WAV（ASR 标准输入格式）；
    - extract_keyframes(): 按固定时间间隔抽样关键帧（PNG）。

P2-C 优化（突破 GB 级视频 OOM 和磁盘打满）：
    - 时长采样：不再用场景检测（全片扫描），改为按固定时间间隔抽帧
      （ffmpeg -ss 跳转 + -frames:v 1 单帧模式，避免全片解码）；
    - 流式删除：黑屏/静态帧抽完立即删除 PNG，不堆积中间产物；
    - 黑屏跳过：计算帧直方图方差，方差低于阈值视为静态画面跳过。

遵循优雅降级：ffmpeg/PIL/numpy 未安装时返回空结果或跳过黑屏检测，
不阻断主流程。遵循开闭原则：新增视频处理能力只需扩展 VideoProcessor 方法。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass

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
        variance: 帧直方图方差（越大画面越丰富；黑屏/静态帧方差趋近 0）。
            依赖缺失或计算失败时为 0.0，表示未做黑屏判定。
    """

    timestamp: float
    image_path: str
    variance: float = 0.0

    @property
    def timestamp_label(self) -> str:
        """格式化时间戳标签 — MM:SS 格式。"""
        return f"{int(self.timestamp // 60):02d}:{int(self.timestamp % 60):02d}"


class VideoProcessor:
    """视频处理器 — 通过 ffmpeg 提取音轨和关键帧。

    ffmpeg 命令说明：
        - 音轨提取：-vn -acodec pcm_s16le -ar 16000 -ac 1
          （丢弃视频，PCM 16-bit，16kHz，单声道 — ASR 标准格式）
        - 关键帧抽取（P2-C 时长采样）：-ss {t} -frames:v 1
          （直接 seek 到时间点 t 抽单帧，不扫描全片，避免 GB 级视频 OOM）
    """

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
        """从视频抽取关键帧（P2-C 时长采样模式）。

        不再使用场景检测（需全片扫描），改为按固定时间间隔抽样：
            1. ffprobe 获取视频时长；
            2. 按 VIDEO_KEYFRAME_INTERVAL 生成采样时间点；
            3. 若点数超过 VIDEO_KEYFRAME_MAX，均匀降采样；
            4. 对每个时间点用 `ffmpeg -ss {t} -frames:v 1` 抽单帧
               （seek + 单帧，不扫描全片）；
            5. 计算帧直方图方差，低于 VIDEO_KEYFRAME_VARIANCE_THRESHOLD
               视为黑屏/静态画面，立即删除 PNG 并跳过（流式删除）。

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

        interval = settings.VIDEO_KEYFRAME_INTERVAL
        max_frames = settings.VIDEO_KEYFRAME_MAX
        variance_threshold = settings.VIDEO_KEYFRAME_VARIANCE_THRESHOLD

        # 1. 获取时长并生成采样时间点
        timestamps = await self._build_sample_timestamps(
            video_path, interval, max_frames
        )
        if not timestamps:
            log.warning("video.keyframes.no_timestamps", path=video_path)
            return []

        output_dir = tempfile.mkdtemp(prefix="keyframes_")
        keyframes: list[KeyFrame] = []

        # 2. 逐时间点 seek 抽单帧 + 方差黑屏跳过（流式删除）
        for t in timestamps:
            frame_path = os.path.join(output_dir, f"frame_{int(t):08d}.png")
            extracted = await self._seek_extract_one(video_path, t, frame_path)
            if not extracted:
                continue  # 抽帧失败已记日志，直接下一个

            variance = self.calculate_frame_variance(frame_path)
            if variance is not None and variance < variance_threshold:
                # 黑屏/静态画面 — 立即删除 PNG，不堆积中间产物
                self._safe_delete(frame_path)
                log.info(
                    "video.keyframes.skip_black_frame",
                    timestamp=t,
                    variance=variance,
                    threshold=variance_threshold,
                )
                continue

            keyframes.append(
                KeyFrame(
                    timestamp=float(t),
                    image_path=frame_path,
                    variance=variance if variance is not None else 0.0,
                )
            )

        log.info(
            "video.keyframes.extracted",
            count=len(keyframes),
            sampled=len(timestamps),
            interval=interval,
            max_frames=max_frames,
        )
        return keyframes

    async def _build_sample_timestamps(
        self,
        video_path: str,
        interval: int,
        max_frames: int,
    ) -> list[float]:
        """按间隔生成采样时间点，超出 max_frames 时均匀降采样。

        Args:
            video_path: 视频文件路径。
            interval: 采样间隔（秒）。
            max_frames: 最大帧数。

        Returns:
            升序时间点列表。时长获取失败时回退为 [0.0]（尽力抽首帧）。
        """
        duration = await self._get_duration(video_path)
        if duration <= 0:
            log.warning("video.keyframes.duration_unknown", path=video_path)
            return [0.0]

        # 按 interval 生成时间点：0, interval, 2*interval, ... < duration
        timestamps = [float(t) for t in range(0, int(duration), interval)]
        if not timestamps:
            timestamps = [0.0]

        # 超过上限则均匀降采样（含首尾，覆盖全片）
        if len(timestamps) > max_frames:
            step = (len(timestamps) - 1) / (max_frames - 1) if max_frames > 1 else 0
            if max_frames > 1:
                indices = [round(i * step) for i in range(max_frames)]
                # 去重保序（极端情况下 round 可能产生重复）
                seen: set[int] = set()
                deduped: list[float] = []
                for idx in indices:
                    if idx not in seen:
                        seen.add(idx)
                        deduped.append(timestamps[idx])
                timestamps = deduped
            else:
                timestamps = [timestamps[0]]

        return timestamps

    async def _seek_extract_one(
        self,
        video_path: str,
        timestamp: float,
        frame_path: str,
    ) -> bool:
        """用 ffmpeg seek 模式抽取单个时间点的帧。

        命令 `ffmpeg -ss {t} -i {video} -frames:v 1 -y {out}`：
            -ss 放在 -i 之前为快速 seek（基于关键点），避免全片解码；
            -frames:v 1 只输出一帧即退出，突破 GB 级视频 OOM。

        Args:
            video_path: 视频文件路径。
            timestamp: seek 时间点（秒）。
            frame_path: 输出 PNG 路径。

        Returns:
            是否成功生成帧文件。
        """
        cmd = [
            "ffmpeg",
            "-ss", f"{timestamp}",
            "-i", video_path,
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
            _, stderr = await process.communicate()
        except FileNotFoundError:
            log.warning("video.keyframes.ffmpeg_not_installed")
            return False
        except Exception as exc:
            log.warning(
                "video.keyframes.seek_failed",
                timestamp=timestamp,
                error=str(exc),
            )
            return False

        if process.returncode != 0 or not os.path.exists(frame_path):
            log.warning(
                "video.keyframes.ffmpeg_error",
                timestamp=timestamp,
                returncode=process.returncode,
                stderr=stderr.decode()[:300] if stderr else "",
            )
            return False
        return True

    def calculate_frame_variance(self, image_path: str) -> float | None:
        """计算图像直方图方差 — 用于黑屏/静态画面检测。

        方差越小画面越静态（纯色/黑屏方差趋近 0）。
        延迟导入 PIL/numpy，未安装时返回 None（优雅降级，跳过黑屏检测）。

        Args:
            image_path: 帧图片文件路径。

        Returns:
            图像灰度方差；依赖缺失或计算失败时返回 None。
        """
        if not image_path or not os.path.exists(image_path):
            return None

        try:
            import numpy as np
            from PIL import Image
        except ImportError:
            # 零依赖 fallback：跳过黑屏检测（不做判定）
            log.info("video.keyframes.variance_deps_missing")
            return None

        try:
            with Image.open(image_path) as img:
                gray = img.convert("L")
                arr = np.asarray(gray, dtype=np.float64)
            if arr.size == 0:
                return 0.0
            return float(arr.var())
        except Exception as exc:
            log.warning("video.keyframes.variance_failed", error=str(exc))
            return None

    def _safe_delete(self, path: str) -> None:
        """安全删除文件 — 忽略不存在或权限错误。"""
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log.warning("video.keyframes.delete_failed", path=path, error=str(exc))

    def cleanup_keyframes(self, keyframes: list[KeyFrame]) -> None:
        """清理关键帧 PNG 文件 — 供 VLM 描述完成后流式删除调用。

        P2-C 流式删除语义：调用方（document_tasks）描述完一帧后即可调用
        本方法删除对应 PNG，避免中间产物堆积打满磁盘。
        本方法不删除 KeyFrame 对象本身，仅清理磁盘文件。

        Args:
            keyframes: 已处理完成的关键帧列表。
        """
        for kf in keyframes:
            self._safe_delete(kf.image_path)

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
