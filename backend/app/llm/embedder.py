"""
Embedding Provider — 单一职责：提供文本向量化服务（双模式）。

- SaaS 模式：OpenAI text-embedding-3-large（3072 维）
- 私有部署（海外/国内）：BGE-M3 via TEI（1024 维，Apache 2.0，海外国内通用）

遵循开闭原则：新增 Embedder 只需继承 EmbeddingProvider 并通过
``register_embedder`` 注册一个工厂函数，无需修改 get_embedder 分支逻辑。
遵循单一职责：本模块只负责向量生成，不涉及 LLM 对话。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.config import get_settings

settings = get_settings()

# Embedder 工厂注册表 — deploy_mode → 工厂函数。
_embedder_registry: dict[str, Callable[[], "EmbeddingProvider"]] = {}


class EmbeddingProvider(ABC):
    """Embedding 统一接口 — 所有向量服务实现本抽象。"""

    #: 输出向量维度，子类覆盖（用于建库时确定 collection dim）。
    dim: int = 0

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """将文本批量向量化，返回与入参等长的向量列表。"""
        raise NotImplementedError


class OpenAIEmbedder(EmbeddingProvider):
    """SaaS 模式 Embedder — OpenAI text-embedding-3-large，3072 维。"""

    dim = 3072
    model = "text-embedding-3-large"

    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in resp.data]


class TEIEmbedder(EmbeddingProvider):
    """私有部署 Embedder — BGE-M3 via HuggingFace TEI，1024 维。

    海外与国内私有部署统一使用 BGE-M3（Apache 2.0，无国别/许可证限制）。
    """

    dim = 1024
    model = "BAAI/bge-m3"

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=60.0)
        self.base_url = settings.tei_embed_url

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self.client.post(
            f"{self.base_url}/embed",
            json={"inputs": texts},
        )
        resp.raise_for_status()
        data: Any = resp.json()
        # TEI /embed 对单条输入返回单层 list，批量返回二层 list，统一规整。
        if texts and isinstance(data, list) and data and isinstance(data[0], (int, float)):
            return [list(map(float, data))]
        return [list(map(float, vec)) for vec in data]


class DashScopeEmbedder(EmbeddingProvider):
    """SaaS·国内 Embedder — 通义千问 text-embedding-v3 via DashScope，1024 维。

    DashScope 提供 OpenAI 兼容接口，复用 ``AsyncOpenAI`` 客户端。
    国内直连无需代理，有新用户免费额度。
    """

    dim = 1024

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=settings.DASHSCOPE_BASE_URL,
            api_key=settings.DASHSCOPE_API_KEY,
        )
        self.model = settings.DASHSCOPE_EMBED_MODEL
        # 动态读取维度配置（text-embedding-v3=1024, v2=1536）
        self.dim = settings.DASHSCOPE_EMBED_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in resp.data]


def register_embedder(
    deploy_mode: str,
) -> Callable[[Callable[[], "EmbeddingProvider"]], Callable[[], "EmbeddingProvider"]]:
    """装饰器：注册某部署模式对应的 Embedder 工厂函数。"""

    def decorator(
        factory: Callable[[], "EmbeddingProvider"],
    ) -> Callable[[], "EmbeddingProvider"]:
        _embedder_registry[deploy_mode] = factory
        return factory

    return decorator


@register_embedder("saas")
def _make_openai_embedder() -> EmbeddingProvider:
    """SaaS：OpenAI text-embedding-3-large。"""
    return OpenAIEmbedder()


@register_embedder("saas_dashscope")
def _make_dashscope_embedder() -> EmbeddingProvider:
    """SaaS·国内：通义千问 text-embedding-v3 via DashScope。"""
    return DashScopeEmbedder()


@register_embedder("private_overseas")
@register_embedder("private_domestic")
def _make_tei_embedder() -> EmbeddingProvider:
    """私有部署（海外/国内）：BGE-M3 via TEI。"""
    return TEIEmbedder()


@lru_cache
def get_embedder() -> EmbeddingProvider:
    """获取当前部署模式的 Embedding Provider（单例，复用底层连接）。

    通过 DEPLOY_MODE 切换：saas → OpenAIEmbedder，
    private_overseas/private_domestic → TEIEmbedder。
    """
    mode = settings.DEPLOY_MODE
    factory = _embedder_registry.get(mode)
    if factory is None:
        raise ValueError(
            f"不支持的 DEPLOY_MODE（embedder）: {mode}，"
            f"已注册: {list(_embedder_registry)}"
        )
    return factory()
