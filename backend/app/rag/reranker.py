"""
重排器 — 单一职责：对检索召回的候选文档重排，提升顶部相关性。

双模式重排：
    - CohereReranker：SaaS 模式，调用 Cohere Rerank API（rerank-multilingual-v3.0）；
    - TEIReranker：私有部署，通过 httpx 调用 TEI Reranker REST API
      （海外 jina-reranker-v2 / 国内 bge-reranker-v2，由 RERANKER_MODEL 决定）；
    - get_reranker()：工厂函数，根据 DEPLOY_MODE 切换。

遵循开闭原则：新增 Reranker 只需继承 RerankerBase 并通过 register_reranker
注册一个工厂函数，无需修改 get_reranker 分支逻辑。
遵循单一职责：本模块只负责重排序，不涉及检索与生成。
遵循优雅降级：重排失败时返回原始顺序（score=0），不抛异常。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()

# Reranker 工厂注册表 — deploy_mode → 工厂函数。
_reranker_registry: dict[str, Callable[[], "RerankerBase"]] = {}


class RerankerBase(ABC):
    """重排器统一接口 — 所有重排实现继承本抽象。"""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str | dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """对文档列表重排，返回按相关性降序排列的结果。

        Args:
            query: 用户查询文本。
            documents: 待重排的文档列表，元素可为纯文本字符串或
                       包含 "content" 字段的 dict。
            top_k: 返回前 top_k 条。

        Returns:
            重排结果列表，每项格式::

                {"index": int, "score": float, "content": str}
        """
        raise NotImplementedError


def _extract_content(doc: str | dict[str, Any]) -> str:
    """从文档项中提取纯文本内容。"""
    if isinstance(doc, str):
        return doc
    if isinstance(doc, dict):
        return str(doc.get("content") or doc.get("chunk_text") or "")
    return str(doc)


class CohereReranker(RerankerBase):
    """SaaS 模式重排器 — Cohere Rerank 3.5（rerank-multilingual-v3.0）。"""

    def __init__(self) -> None:
        import cohere

        self.client = cohere.AsyncClientV2(api_key=settings.COHERE_API_KEY)
        self.model = "rerank-multilingual-v3.0"

    async def rerank(
        self,
        query: str,
        documents: list[str | dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        if not documents:
            return []
        texts = [_extract_content(d) for d in documents]
        try:
            resp = await self.client.rerank(
                model=self.model,
                query=query,
                documents=texts,
                top_n=min(top_k, len(texts)),
            )
            results: list[dict[str, Any]] = []
            for item in resp.results:
                idx = item.index
                results.append(
                    {
                        "index": idx,
                        "score": float(item.relevance_score),
                        "content": texts[idx] if idx < len(texts) else "",
                    }
                )
            log.debug("reranker.cohere", count=len(results))
            return results
        except Exception as exc:
            log.warning("reranker.cohere.error", error=str(exc))
            return self._fallback(documents, top_k)

    @staticmethod
    def _fallback(
        documents: list[str | dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Cohere 不可用时回退 — 保留原始顺序，score 置 0。"""
        return [
            {"index": i, "score": 0.0, "content": _extract_content(d)}
            for i, d in enumerate(documents[:top_k])
        ]


class TEIReranker(RerankerBase):
    """私有部署重排器 — 通过 TEI Reranker REST API。

    海外用 jinaai/jina-reranker-v2-base-multilingual，国内用 BAAI/bge-reranker-v2-m3，
    由 settings.RERANKER_MODEL 决定，本类不感知具体模型。
    """

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=10.0)
        self.base_url = settings.tei_reranker_url

    async def rerank(
        self,
        query: str,
        documents: list[str | dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        if not documents:
            return []
        texts = [_extract_content(d) for d in documents]
        payload: dict[str, Any] = {
            "query": query,
            "texts": texts,
            "top_k": min(top_k, len(texts)),
        }
        try:
            resp = await self.client.post(
                f"{self.base_url}/rerank",
                json=payload,
            )
            resp.raise_for_status()
            data: Any = resp.json()
            return self._parse_tei_response(data, texts, top_k)
        except Exception as exc:
            log.warning("reranker.tei.error", error=str(exc))
            return self._fallback(documents, top_k)

    @staticmethod
    def _parse_tei_response(
        data: Any,
        texts: list[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """解析 TEI Reranker 返回结果。

        TEI /rerank 返回格式::

            [{"index": 0, "score": 0.98}, ...]
        """
        results: list[dict[str, Any]] = []
        rows: list[Any] = data if isinstance(data, list) else data.get("results", [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx = int(row.get("index", 0))
            results.append(
                {
                    "index": idx,
                    "score": float(row.get("score", 0.0)),
                    "content": texts[idx] if idx < len(texts) else "",
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]

    @staticmethod
    def _fallback(
        documents: list[str | dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """TEI 不可用时回退 — 保留原始顺序，score 置 0。"""
        return [
            {"index": i, "score": 0.0, "content": _extract_content(d)}
            for i, d in enumerate(documents[:top_k])
        ]

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self.client.aclose()


# ------------------------------------------------------------------
# 注册表 — 开闭原则落点
# ------------------------------------------------------------------


def register_reranker(
    deploy_mode: str,
) -> Callable[[Callable[[], "RerankerBase"]], Callable[[], "RerankerBase"]]:
    """装饰器：注册某部署模式对应的 Reranker 工厂函数。"""

    def decorator(
        factory: Callable[[], "RerankerBase"],
    ) -> Callable[[], "RerankerBase"]:
        _reranker_registry[deploy_mode] = factory
        return factory

    return decorator


@register_reranker("saas")
def _make_cohere_reranker() -> RerankerBase:
    """SaaS：Cohere Rerank 3.5。"""
    return CohereReranker()


@register_reranker("private_overseas")
@register_reranker("private_domestic")
def _make_tei_reranker() -> RerankerBase:
    """私有部署（海外/国内）：TEI Reranker（Jina/BGE，由 RERANKER_MODEL 决定）。"""
    return TEIReranker()


@lru_cache
def get_reranker() -> RerankerBase:
    """获取当前部署模式的重排器（单例，复用底层连接）。

    通过 DEPLOY_MODE 切换：saas → CohereReranker，
    private_overseas/private_domestic → TEIReranker。

    Raises:
        ValueError: DEPLOY_MODE 未在注册表中。
    """
    mode = settings.DEPLOY_MODE
    factory = _reranker_registry.get(mode)
    if factory is None:
        raise ValueError(
            f"不支持的 DEPLOY_MODE（reranker）: {mode}，"
            f"已注册: {list(_reranker_registry)}"
        )
    return factory()
