"""
VLM 视觉处理层 — 对外暴露统一的图像理解接口。

对外暴露工厂函数 ``get_vision_provider`` 及统一类型，
业务层仅依赖本包导出的抽象，按 DEPLOY_MODE 切换底层实现，业务代码零改动。

典型用法::

    from app.vlm import get_vision_provider

    provider = get_vision_provider()
    description = await provider.understand(image_bytes, prompt="描述这张图片")
"""

from __future__ import annotations

from app.vlm.provider import (
    AnthropicVisionProvider,
    VisionProvider,
    VLLMVisionProvider,
    get_vision_provider,
)

__all__ = [
    "get_vision_provider",
    "VisionProvider",
    "AnthropicVisionProvider",
    "VLLMVisionProvider",
]
