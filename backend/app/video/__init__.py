"""
视频处理层 — 对外暴露音轨提取和关键帧抽取接口。
"""

from __future__ import annotations

from app.video.processor import (
    KeyFrame,
    VideoProcessor,
    get_video_processor,
)

__all__ = [
    "KeyFrame",
    "VideoProcessor",
    "get_video_processor",
]
