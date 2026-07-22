"""
混合检索器 — 单一职责：多路召回知识库文档（向量 + 全文）。

实现 Hybrid Retrieval：
    - 向量检索：通过 VectorStoreBase 适配器检索（默认 OpenSearch k-NN，可选 Milvus）；
    - 全文检索：通过 OpenSearch 检索（BM25）；
    - 合并结果并去重（按 chunk_id）。

遵循单一职责：本模块只负责召回候选文档，不涉及重排与生成。
遵循依赖倒置：向量后端通过 VectorStoreBase 抽象注入，可替换为 Mock；
             全文后端通过 http_client 注入，可替换为 Mock。
遵循优雅降级：向量存储 / OpenSearch 任一不可用时返回空列表并记录日志，
不抛异常，确保 RAG 引擎可继续运行（仅召回能力下降）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from app.config import get_settings
from app.llm.embedder import EmbeddingProvider, get_embedder
from app.rag.vector_store import VectorStoreBase, get_vector_store
from app.utils.logger import get_logger

log = get_logger(__name__)

settings = get_settings()

# 检索超时（秒）— 用于全文检索的 HTTP 客户端
_SEARCH_TIMEOUT: float = 5.0

# OpenSearch 故障后的重试探测间隔（秒）— 避免一次失败永久禁用 BM25
_OPENSEARCH_RETRY_INTERVAL: float = 30.0


class HybridRetriever:
    """混合检索器 — 向量 + 全文多路召回后合并去重。

    使用方式::

        retriever = HybridRetriever()
        candidates = await retriever.search("报销流程", kb_ids=[...], top_k=20)

    向量后端通过 VECTOR_STORE 环境变量切换（os_knn / milvus），
    也可通过构造函数显式注入 VectorStoreBase 实例（测试场景）。
    """

    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
        vector_store: VectorStoreBase | None = None,
    ) -> None:
        self._embedder: EmbeddingProvider | None = embedder
        from app.utils.retry import build_retry_http_client

        self._http: httpx.AsyncClient = http_client or build_retry_http_client(
            timeout=_SEARCH_TIMEOUT
        )
        self._vector_store: VectorStoreBase | None = vector_store
        self._opensearch_available: bool | None = None
        # 故障后的下次重试探测时间（monotonic）— 失败不粘性禁用，允许自动恢复
        self._opensearch_retry_at: float = 0.0

    # ------------------------------------------------------------------
    # 懒初始化
    # ------------------------------------------------------------------

    async def _get_embedder(self) -> EmbeddingProvider | None:
        """懒初始化 Embedder — 失败则返回 None。"""
        if self._embedder is not None:
            return self._embedder
        try:
            self._embedder = get_embedder()
        except Exception as exc:
            log.warning("retriever.embedder.unavailable", error=str(exc))
        return self._embedder

    def _get_vector_store(self) -> VectorStoreBase:
        """懒初始化向量存储 — 未注入时通过工厂获取单例。"""
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """多路检索知识库，返回合并去重后的候选文档列表。

        Args:
            query: 用户查询文本。
            kb_ids: 可选，限定检索的知识库 ID 列表。
            top_k: 每路召回数量上限（合并前）。

        Returns:
            候选文档列表，每项格式::

                {
                    "doc_id": str,
                    "chunk_id": str,
                    "content": str,
                    "score": float,
                    "source": "vector" | "fulltext",
                    "kb_id": str | None,
                    "title": str | None,
                }
        """
        if not query.strip():
            return []

        # 并发执行两路检索（任一失败返回空列表，不影响另一路）
        vector_results = await self._vector_search(query, kb_ids, top_k)
        fulltext_results = await self._fulltext_search(query, kb_ids, top_k)

        merged = self._merge_and_dedupe(vector_results, fulltext_results, top_k)
        log.info(
            "retriever.search",
            query_len=len(query),
            vector_count=len(vector_results),
            fulltext_count=len(fulltext_results),
            merged_count=len(merged),
        )
        return merged

    # ------------------------------------------------------------------
    # 向量检索（通过 VectorStoreBase 适配器）
    # ------------------------------------------------------------------

    async def _vector_search(
        self,
        query: str,
        kb_ids: list[str] | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """通过 VectorStoreBase 适配器执行向量检索。

        向量后端由 VECTOR_STORE 配置决定（默认 OpenSearch k-NN，可选 Milvus）。
        适配器的 search 经熔断器保护：后端故障时异常向上传播（熔断器
        记录失败，OPEN 后快速拒绝），此处捕获并降级为空列表。
        """
        embedder = await self._get_embedder()
        if embedder is None:
            return []

        try:
            query_vec = (await embedder.embed([query]))[0]
        except Exception as exc:
            log.warning("retriever.vector.embed_error", error=str(exc))
            return []

        store = self._get_vector_store()
        try:
            return await store.search(query_vec, kb_ids=kb_ids, top_k=top_k)
        except Exception as exc:
            log.warning("retriever.vector.search_failed", error=str(exc))
            return []

    # ------------------------------------------------------------------
    # 全文检索（OpenSearch BM25）
    # ------------------------------------------------------------------

    async def _fulltext_search(
        self,
        query: str,
        kb_ids: list[str] | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """通过 OpenSearch REST API 执行全文检索（BM25）。

        索引名取自统一配置 ``settings.OPENSEARCH_INDEX``，与写入方保持一致；
        失败后按 ``_OPENSEARCH_RETRY_INTERVAL`` 间隔重试探测，可自动恢复，
        不做一次性永久禁用。
        """
        if self._opensearch_available is False:
            if time.monotonic() < self._opensearch_retry_at:
                return []
            log.info("retriever.opensearch.retry_probe")

        url = f"{settings.OPENSEARCH_URL}/{settings.OPENSEARCH_INDEX}/_search"
        query_clause: dict[str, Any] = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["chunk_text", "title^2"],
                        }
                    }
                ],
            }
        }
        if kb_ids:
            query_clause["bool"]["filter"] = [{"terms": {"kb_id": kb_ids}}]

        payload: dict[str, Any] = {
            "size": top_k,
            "query": query_clause,
            "_source": ["doc_id", "chunk_text", "kb_id", "title"],
        }

        try:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            data: Any = resp.json()
            self._opensearch_available = True
            self._opensearch_retry_at = 0.0
        except Exception as exc:
            if self._opensearch_available is not False:
                log.warning("retriever.opensearch.unavailable", error=str(exc))
            self._opensearch_available = False
            # 设定下次重试时间，超时后自动重新探测（非粘性禁用）
            self._opensearch_retry_at = time.monotonic() + _OPENSEARCH_RETRY_INTERVAL
            return []

        return self._parse_opensearch_results(data, top_k)

    @staticmethod
    def _parse_opensearch_results(data: Any, top_k: int) -> list[dict[str, Any]]:
        """解析 OpenSearch 返回结果为统一格式。"""
        results: list[dict[str, Any]] = []
        hits: Any = {}
        if isinstance(data, dict):
            hits = data.get("hits", {}).get("hits", []) or []
        for hit in hits:
            source: Any = hit.get("_source", {}) if isinstance(hit, dict) else {}
            score = float(hit.get("_score", 0.0)) if isinstance(hit, dict) else 0.0
            results.append(
                {
                    "doc_id": str(source.get("doc_id") or hit.get("_id") or ""),
                    "chunk_id": str(source.get("chunk_id") or hit.get("_id") or uuid.uuid4()),
                    "content": str(source.get("chunk_text") or source.get("content") or ""),
                    "score": score,
                    "source": "fulltext",
                    "kb_id": str(source.get("kb_id") or "") or None,
                    "title": source.get("title"),
                }
            )
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------
    # 合并去重
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_and_dedupe(
        vector_results: list[dict[str, Any]],
        fulltext_results: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """合并多路结果并按 chunk_id 去重，保留最高分。

        同一 chunk_id 在两路均命中时，取较高 score 并标注首次命中的来源。
        """
        merged: dict[str, dict[str, Any]] = {}
        for doc in vector_results:
            cid = doc["chunk_id"]
            if cid not in merged or doc["score"] > merged[cid]["score"]:
                merged[cid] = dict(doc)
        for doc in fulltext_results:
            cid = doc["chunk_id"]
            if cid not in merged:
                merged[cid] = dict(doc)
            else:
                # 已存在则合并分数（取较高），保留来源标记
                if doc["score"] > merged[cid]["score"]:
                    merged[cid]["score"] = doc["score"]
        # 按 score 降序排列
        ranked = sorted(merged.values(), key=lambda d: d["score"], reverse=True)
        return ranked[:top_k]

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self._http.aclose()
