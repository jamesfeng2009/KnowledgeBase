"""
OpenSearch k-NN 向量存储实现 — 默认后端。

利用 OpenSearch 的 k-NN 插件（nmslib / faiss 引擎）实现向量检索，
与 BM25 全文检索共享同一 OpenSearch 集群，运维简单。

适用场景：
    - 向量规模 < 500 万（企业知识库典型规模）；
    - 希望减少基础设施组件（向量 + 全文共用 OpenSearch）；
    - SaaS 多租户场景（按 kb_id 过滤）。

索引结构（ekb_knn_vectors）::

    {
        "mappings": {
            "properties": {
                "doc_id":          {"type": "keyword"},
                "chunk_id":        {"type": "keyword"},
                "content":         {"type": "text", "analyzer": "standard"},
                "embedding":       {"type": "knn_vector", "dimension": 1024,
                                    "method": {"name": "hnsw",
                                               "space_type": "cosinesimil",
                                               "engine": "nmslib"}},
                "kb_id":           {"type": "keyword"},
                "title_path":      {"type": "text", "analyzer": "keyword"},
                "content_type":    {"type": "keyword"},
                "chunk_strategy":  {"type": "keyword"}
            }
        }
    }

降级策略：
    - search 经 ``@circuit_call`` 熔断器保护，异常向上传播以记录失败；
      熔断 OPEN 后快速拒绝（CircuitBreakerOpenError），由调用方
      （retriever）捕获并降级为空列表；
    - upsert / delete / health_check 遵循优雅降级，不可用时返回 0 / None。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from app.config import get_settings
from app.rag.vector_store.base import VectorStoreBase
from app.utils.circuit_breaker import circuit_call
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.rag.chunker import Chunk

log = get_logger(__name__)

# k-NN 向量索引名（与全文索引 ekb_documents 分离，避免 mapping 冲突）
_KNN_INDEX: str = "ekb_knn_vectors"
# 请求超时（秒）
_TIMEOUT: float = 10.0


class OpenSearchVectorStore(VectorStoreBase):
    """OpenSearch k-NN 向量存储 — 默认实现。

    通过 REST API 操作 OpenSearch k-NN 索引，无需额外客户端库依赖。
    索引在首次 upsert 时自动创建（如果不存在）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        index_name: str = _KNN_INDEX,
    ) -> None:
        from app.utils.retry import build_retry_http_client

        self._http: httpx.AsyncClient = http_client or build_retry_http_client(
            timeout=_TIMEOUT
        )
        self._index_name: str = index_name
        self._available: bool | None = None
        self._index_ready: bool = False

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    @circuit_call("vectorstore_opensearch")
    async def search(
        self,
        query_vec: list[float],
        kb_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """通过 OpenSearch k-NN 执行向量相似度检索 — 异常向上传播以触发熔断器。"""
        if self._available is False:
            return []

        import time
        t0 = time.monotonic()
        settings = get_settings()
        url = f"{settings.OPENSEARCH_URL}/{self._index_name}/_search"

        # 构建 k-NN 查询
        knn_clause: dict[str, Any] = {
            "knn": {
                "embedding": {
                    "vector": query_vec,
                    "k": top_k,
                }
            }
        }

        if kb_ids:
            query_body: dict[str, Any] = {
                "bool": {
                    "must": [knn_clause],
                    "filter": [{"terms": {"kb_id": kb_ids}}],
                }
            }
        else:
            query_body = knn_clause

        payload: dict[str, Any] = {
            "size": top_k,
            "query": query_body,
            "_source": [
                "doc_id",
                "chunk_id",
                "content",
                "kb_id",
                "title_path",
                "content_type",
                "chunk_strategy",
            ],
        }

        log.info("vector_store.os_knn.search_start", top_k=top_k, has_kb_filter=bool(kb_ids))
        resp = await self._http.post(url, json=payload)
        resp.raise_for_status()
        data: Any = resp.json()
        self._available = True
        results = self._parse_results(data, top_k)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        log.info("vector_store.os_knn.search_done", count=len(results), latency_ms=elapsed_ms)
        return results

    @staticmethod
    def _parse_results(data: Any, top_k: int) -> list[dict[str, Any]]:
        """解析 OpenSearch k-NN 返回结果为统一格式。"""
        results: list[dict[str, Any]] = []
        hits: Any = []
        if isinstance(data, dict):
            hits = data.get("hits", {}).get("hits", []) or []

        for hit in hits:
            if not isinstance(hit, dict):
                continue
            source: Any = hit.get("_source", {})
            score = float(hit.get("_score", 0.0))
            results.append(
                VectorStoreBase._format_result(
                    doc_id=str(source.get("doc_id") or ""),
                    chunk_id=str(source.get("chunk_id") or hit.get("_id") or ""),
                    content=str(source.get("content") or ""),
                    score=score,
                    kb_id=str(source.get("kb_id") or "") or None,
                    title=source.get("title_path") or None,
                )
            )
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------
    # upsert
    # ------------------------------------------------------------------

    async def upsert(
        self,
        doc_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        kb_id: str | None = None,
    ) -> int:
        """批量写入向量数据到 OpenSearch k-NN 索引。

        ``kb_id`` 字段写入文档所属知识库 ID（入参或 chunk 携带），
        与检索端按知识库过滤对齐；历史 bug 曾错误写入 doc_id。
        """
        if not embeddings or not chunks:
            return 0

        # 确保索引存在
        if not self._index_ready:
            await self._ensure_index()
            self._index_ready = True

        if self._available is False:
            return 0

        settings = get_settings()
        url = f"{settings.OPENSEARCH_URL}/{self._index_name}/_bulk"

        # 构建 bulk 请求体（NDJSON 格式）
        n = min(len(embeddings), len(chunks))
        lines: list[str] = []
        for i in range(n):
            chunk = chunks[i]
            action = {"index": {"_index": self._index_name, "_id": chunk.id}}
            doc_body: dict[str, Any] = {
                "doc_id": doc_id,
                "chunk_id": chunk.id,
                "content": chunk.content,
                "embedding": embeddings[i],
                "kb_id": self._resolve_kb_id(chunk, doc_id, kb_id),
                "title_path": chunk.title_path,
                "content_type": chunk.content_type,
                "chunk_strategy": chunk.chunk_strategy,
            }
            lines.append(json.dumps(action, ensure_ascii=False))
            lines.append(json.dumps(doc_body, ensure_ascii=False))

        body = "\n".join(lines) + "\n"

        try:
            resp = await self._http.post(
                url,
                content=body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            resp.raise_for_status()
            self._available = True
            log.info(
                "vector_store.os_knn.upserted",
                doc_id=doc_id,
                count=n,
            )
            return n
        except Exception as exc:
            if self._available is not False:
                log.warning("vector_store.os_knn.upsert_failed", error=str(exc))
            self._available = False
            return 0

    async def _ensure_index(self) -> None:
        """确保 k-NN 索引存在，不存在则创建。"""
        settings = get_settings()
        url = f"{settings.OPENSEARCH_URL}/{self._index_name}"

        try:
            resp = await self._http.head(url)
            if resp.status_code == 200:
                self._available = True
                return
        except Exception:
            pass

        # 创建索引
        index_body: dict[str, Any] = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 100,
                }
            },
            "mappings": {
                "properties": {
                    "doc_id": {"type": "keyword"},
                    "chunk_id": {"type": "keyword"},
                    "content": {"type": "text", "analyzer": "standard"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": self.dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "nmslib",
                            "parameters": {"ef_construction": 128, "m": 24},
                        },
                    },
                    "kb_id": {"type": "keyword"},
                    "title_path": {"type": "text", "analyzer": "keyword"},
                    "content_type": {"type": "keyword"},
                    "chunk_strategy": {"type": "keyword"},
                }
            },
        }

        try:
            resp = await self._http.put(url, json=index_body)
            resp.raise_for_status()
            self._available = True
            log.info("vector_store.os_knn.index_created", index=self._index_name)
        except Exception as exc:
            log.warning("vector_store.os_knn.index_create_failed", error=str(exc))
            self._available = False

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, doc_id: str) -> None:
        """按 doc_id 删除所有向量文档。"""
        if self._available is False:
            return

        settings = get_settings()
        url = f"{settings.OPENSEARCH_URL}/{self._index_name}/_delete_by_query"

        payload: dict[str, Any] = {
            "query": {"term": {"doc_id": doc_id}},
        }

        try:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            self._available = True
            log.info("vector_store.os_knn.deleted", doc_id=doc_id)
        except Exception as exc:
            if self._available is not False:
                log.warning("vector_store.os_knn.delete_failed", error=str(exc))
            self._available = False

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """检查 OpenSearch 服务是否可用。"""
        settings = get_settings()
        url = f"{settings.OPENSEARCH_URL}/_cluster/health"

        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            self._available = True
            return True
        except Exception as exc:
            log.warning("vector_store.os_knn.health_check_failed", error=str(exc))
            self._available = False
            return False

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self._http.aclose()
