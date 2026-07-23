"""
跨模态 Embedding Provider — 单一职责：提供文本+图片统一向量化服务。

基于 jina-clip-v2 模型，支持将文本和图片映射到同一向量空间，
实现跨模态检索（用文本查询检索图片，或用图片检索文本）。

核心能力：
    - embed(texts) → 文本向量（兼容 EmbeddingProvider 接口）
    - embed_images(images) → 图片向量
    - 文本和图片向量在同一空间，可直接计算相似度

适用场景：
    - 文档中的图表/示意图/流程图直接向量化入库；
    - 用户用文本查询检索到相关图片（无需 VLM 中间描述）；
    - 图片和文本混合检索，提升多模态文档的召回率。

遵循开闭原则：新增跨模态模型只需继承 MultimodalEmbeddingProvider，
通过 register_multimodal_embedder 注册工厂函数。
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import httpx

from app.config import get_settings
from app.llm.embedder import EmbeddingProvider
from app.utils.circuit_breaker import circuit_call
from app.utils.logger import get_logger

settings = get_settings()

log = get_logger(__name__)

# 跨模态 Embedder 工厂注册表
_multimodal_registry: dict[str, Callable[[], "MultimodalEmbeddingProvider"]] = {}


class MultimodalEmbeddingProvider(EmbeddingProvider, ABC):
    """跨模态 Embedding 统一接口 — 继承文本 Embedding 并扩展图片向量化。

    文本侧兼容 EmbeddingProvider.embed()，
    图片侧新增 embed_images() 方法。
    """

    @abstractmethod
    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        """将图片批量向量化，返回与入参等长的向量列表。

        Args:
            images: 图片二进制数据列表。

        Returns:
            向量列表，维度与 embed() 输出一致（同一向量空间）。
        """
        raise NotImplementedError


class JinaCLIPEmbedder(MultimodalEmbeddingProvider):
    """Jina CLIP v2 跨模态 Embedder — 文本+图片统一向量空间。

    jina-clip-v2 输出 1024 维向量，文本和图片共享同一嵌入空间，
    支持 text-to-image / image-to-text 跨模态检索。
    API 文档：https://jina.ai/embeddings/
    """

    dim = 1024
    model = "jina-clip-v2"
    _API_URL = "https://api.jina.ai/v1/embeddings"

    def __init__(self) -> None:
        self._api_key = settings.JINA_API_KEY
        self._model = settings.JINA_CLIP_MODEL
        self.dim = settings.JINA_CLIP_DIM
        from app.utils.retry import build_retry_http_client

        self._http = build_retry_http_client(timeout=60.0)

    @circuit_call("embedder_jina_clip")
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """文本向量化 — 兼容 EmbeddingProvider 接口。"""
        if not texts:
            return []
        import time
        t0 = time.monotonic()
        log.info("embedder.jina_clip.text_start", text_count=len(texts))
        try:
            resp = await self._http.post(
                self._API_URL,
                json={
                    "model": self._model,
                    "input": texts,
                    "input_type": "text",
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
            data: Any = resp.json()
            result = [item["embedding"] for item in data.get("data", [])]
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.info("embedder.jina_clip.text_success", dim=self.dim, latency_ms=elapsed_ms)
            return result
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.warning("embedder.jina_clip.text_error", error=str(exc), latency_ms=elapsed_ms)
            raise

    @circuit_call("embedder_jina_clip_image")
    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        """图片向量化 — 与文本向量在同一嵌入空间。"""
        if not images:
            return []
        import time
        t0 = time.monotonic()
        log.info("embedder.jina_clip.image_start", image_count=len(images))
        try:
            # jina-clip-v2 接受 base64 编码的图片
            inputs: list[dict[str, str]] = []
            for img in images:
                b64 = base64.b64encode(img).decode("utf-8")
                inputs.append({"image": b64})

            resp = await self._http.post(
                self._API_URL,
                json={
                    "model": self._model,
                    "input": inputs,
                    "input_type": "image",
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
            data: Any = resp.json()
            result = [item["embedding"] for item in data.get("data", [])]
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.info("embedder.jina_clip.image_success", dim=self.dim, latency_ms=elapsed_ms)
            return result
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            log.warning("embedder.jina_clip.image_error", error=str(exc), latency_ms=elapsed_ms)
            raise

    async def close(self) -> None:
        await self._http.aclose()


# ------------------------------------------------------------------
# 注册表 — 开闭原则落点
# ------------------------------------------------------------------


def register_multimodal_embedder(
    deploy_mode: str,
) -> Callable[[Callable[[], "MultimodalEmbeddingProvider"]], Callable[[], "MultimodalEmbeddingProvider"]]:
    """装饰器：注册某部署模式对应的多模态 Embedder 工厂函数。"""

    def decorator(
        factory: Callable[[], "MultimodalEmbeddingProvider"],
    ) -> Callable[[], "MultimodalEmbeddingProvider"]:
        _multimodal_registry[deploy_mode] = factory
        return factory

    return decorator


@register_multimodal_embedder("saas")
@register_multimodal_embedder("saas_dashscope")
@register_multimodal_embedder("private_overseas")
@register_multimodal_embedder("private_domestic")
def _make_jina_clip_embedder() -> MultimodalEmbeddingProvider:
    """所有部署模式统一使用 Jina CLIP v2（云端 API，免费额度）。"""
    return JinaCLIPEmbedder()


@lru_cache
def get_multimodal_embedder() -> MultimodalEmbeddingProvider | None:
    """获取跨模态 Embedder 单例 — CROSS_MODAL_ENABLED=False 时返回 None。

    Returns:
        MultimodalEmbeddingProvider 实例，或 None（功能未启用）。
    """
    if not settings.CROSS_MODAL_ENABLED:
        return None
    if not settings.JINA_API_KEY:
        log.warning("embedder.multimodal.no_api_key")
        return None
    mode = settings.DEPLOY_MODE
    factory = _multimodal_registry.get(mode)
    if factory is None:
        log.warning("embedder.multimodal.no_factory", deploy_mode=mode)
        return None
    return factory()


__all__ = [
    "MultimodalEmbeddingProvider",
    "JinaCLIPEmbedder",
    "get_multimodal_embedder",
    "register_multimodal_embedder",
]
