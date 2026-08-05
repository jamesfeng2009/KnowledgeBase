"""
VLM 视觉处理层 — 单一职责：理解图片内容并返回文本描述。

双模式实现：
    - AnthropicVisionProvider：SaaS 模式，复用 LLM Provider 的原生多模态能力
      （Claude / GPT 原生视觉），无需独立 VLM 服务；
    - VLLMVisionProvider：私有部署，调用独立 VLM 服务
      （vLLM OpenAI 兼容 API，Pixtral / Qwen2.5-VL），数据不出企业网络；
    - get_vision_provider()：工厂函数，根据 DEPLOY_MODE 切换。

遵循开闭原则：新增 VisionProvider 只需继承并通过 register_vision_provider 注册，
无需修改 get_vision_provider 分支逻辑。
遵循单一职责：本模块只负责图像理解，不涉及检索与生成。
遵循优雅降级：VLM 服务不可用时返回错误提示而非抛异常。
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings
from app.llm.base import LLMProvider
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()

# Vision Provider 工厂注册表 — deploy_mode → 工厂函数。
_vision_registry: dict[str, Callable[[], "VisionProvider"]] = {}


class VisionProvider(ABC):
    """视觉理解统一接口 — 所有 VLM 实现继承本抽象。"""

    @abstractmethod
    async def understand(
        self,
        image: bytes,
        prompt: str,
        mime_type: str = "image/png",
    ) -> str:
        """理解图片内容并返回文本描述。

        Args:
            image: 图片二进制数据。
            prompt: 引导理解的提示词（如"描述这张图片的内容"）。
            mime_type: 图片 MIME 类型（image/png / image/jpeg 等）。

        Returns:
            图片理解结果文本。
        """
        raise NotImplementedError

    async def understand_structured(
        self,
        image: bytes,
        image_type: str = "general",
        mime_type: str = "image/png",
    ) -> dict[str, Any]:
        """结构化理解（P0-5）— JSON schema 输出 + 校验层 + prompt 路由。

        按图片类型路由专用 prompt，统一要求 JSON 输出（状态枚举判定代替
        自由生成）；解析后对数值字段做范围规则校验，越界标记
        ``low_confidence``，由调用方决定降级处理（而非幻觉内容直接入库）。

        基类默认实现：prompt 约束 + JSON 提取，对所有 Provider 生效，
        无需子类改动（开闭原则）。

        Args:
            image: 图片二进制数据。
            image_type: 图片类型（drawing / handwriting / chart / table /
                scanned_text / whiteboard / general）。
            mime_type: 图片 MIME 类型。

        Returns:
            StructuredImageResult.to_dict() 格式字典，含
            status / description / tags / numbers / low_confidence / issues。
        """
        from app.vlm.structured import build_structured_prompt, parse_structured

        raw = await self.understand(
            image=image,
            prompt=build_structured_prompt(image_type),
            mime_type=mime_type,
        )
        return parse_structured(raw, image_type).to_dict()


class AnthropicVisionProvider(VisionProvider):
    """SaaS 模式视觉处理 — 复用 LLM Provider 的原生多模态能力。

    Claude / GPT 原生支持图像输入，无需独立 VLM 服务。
    通过 LLMProvider.chat 传递多模态消息，复用已有 SDK 连接。
    """

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm

    async def understand(
        self,
        image: bytes,
        prompt: str,
        mime_type: str = "image/png",
    ) -> str:
        b64 = base64.b64encode(image).decode("utf-8")
        # Anthropic 多模态 content block 格式
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": b64,
                },
            },
            {"type": "text", "text": prompt},
        ]
        # 复用 LLM Provider，content 以多模态块列表形式传入
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]

        result = ""
        provider = self._get_provider()
        try:
            async for chunk in provider.chat(messages, stream=True, max_tokens=2048):  # type: ignore[arg-type]
                if isinstance(chunk, str):
                    result += chunk
        except Exception as exc:
            log.error("vlm.anthropic.error", error=str(exc))
            return f"[图像理解失败: {exc}]"
        log.info("vlm.anthropic.done", prompt_len=len(prompt), result_len=len(result))
        return result

    def _get_provider(self) -> LLMProvider:
        """获取 LLM Provider — 懒加载，避免导入期循环依赖。"""
        if self.llm is not None:
            return self.llm
        from app.llm.factory import get_llm_provider

        self.llm = get_llm_provider()
        return self.llm


class VLLMVisionProvider(VisionProvider):
    """私有部署视觉处理 — 调用独立 VLM 服务（vLLM OpenAI 兼容 API）。

    海外用 Pixtral 12B，国内用 Qwen2.5-VL，由 settings.VLM_MODEL 决定。
    通过 OpenAI SDK 调用 vLLM 暴露的 OpenAI 兼容 API（data URL 格式图像）。
    """

    def __init__(self, model: str | None = None) -> None:
        base_url = f"http://{settings.VLM_HOST}:{settings.VLM_PORT}/v1"
        # vLLM 本地部署无需真实鉴权，api_key 占位
        self.client = AsyncOpenAI(base_url=base_url, api_key="dummy")
        self.model = model or settings.VLM_MODEL

    async def understand(
        self,
        image: bytes,
        prompt: str,
        mime_type: str = "image/png",
    ) -> str:
        b64 = base64.b64encode(image).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"
        # OpenAI 兼容多模态格式
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        result = ""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=2048,
                stream=True,
            )
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    result += delta.content
        except Exception as exc:
            log.error("vlm.vllm.error", error=str(exc), model=self.model)
            return f"[图像理解失败: {exc}]"
        log.info("vlm.vllm.done", model=self.model, result_len=len(result))
        return result


class DashScopeVisionProvider(VLLMVisionProvider):
    """SaaS·国内视觉处理 — 阿里云 DashScope qwen-vl 系列。

    qwen-vl 模型提供 OpenAI 兼容多模态接口（image_url data URL 格式），
    与 vLLM 的请求格式一致，故直接复用 VLLMVisionProvider.understand，
    仅覆盖 __init__ 指向 DashScope endpoint 与真实 API Key。
    """

    def __init__(self, model: str | None = None) -> None:
        self.client = AsyncOpenAI(
            base_url=settings.DASHSCOPE_BASE_URL,
            api_key=settings.DASHSCOPE_API_KEY,
        )
        self.model = model or settings.DASHSCOPE_VLM_MODEL


# ------------------------------------------------------------------
# 注册表 — 开闭原则落点
# ------------------------------------------------------------------


def register_vision_provider(
    deploy_mode: str,
) -> Callable[[Callable[[], "VisionProvider"]], Callable[[], "VisionProvider"]]:
    """装饰器：注册某部署模式对应的 VisionProvider 工厂函数。"""

    def decorator(
        factory: Callable[[], "VisionProvider"],
    ) -> Callable[[], "VisionProvider"]:
        _vision_registry[deploy_mode] = factory
        return factory

    return decorator


@register_vision_provider("saas")
def _make_anthropic_vision() -> VisionProvider:
    """SaaS：复用 LLM Provider 原生多模态（Claude / GPT 视觉）。"""
    return AnthropicVisionProvider()


@register_vision_provider("saas_dashscope")
def _make_dashscope_vision() -> VisionProvider:
    """SaaS·国内：DashScope qwen-vl 系列（OpenAI 兼容多模态接口）。"""
    return DashScopeVisionProvider()


@register_vision_provider("private_overseas")
@register_vision_provider("private_domestic")
def _make_vllm_vision() -> VisionProvider:
    """私有部署（海外/国内）：独立 VLM 服务（Pixtral / Qwen2.5-VL via vLLM）。"""
    return VLLMVisionProvider()


@lru_cache
def get_vision_provider() -> VisionProvider:
    """获取当前部署模式的视觉处理 Provider（单例，复用底层连接）。

    通过 DEPLOY_MODE 切换：saas → AnthropicVisionProvider（复用 LLM），
    private_overseas/private_domestic → VLLMVisionProvider（独立 VLM）。

    Raises:
        ValueError: DEPLOY_MODE 未在注册表中。
    """
    mode = settings.DEPLOY_MODE
    factory = _vision_registry.get(mode)
    if factory is None:
        raise ValueError(
            f"不支持的 DEPLOY_MODE（vision）: {mode}，"
            f"已注册: {list(_vision_registry)}"
        )
    return factory()


__all__ = [
    "VisionProvider",
    "AnthropicVisionProvider",
    "VLLMVisionProvider",
    "get_vision_provider",
]
