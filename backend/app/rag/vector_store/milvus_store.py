"""
Milvus 向量存储实现 — 可选后端。

利用 Milvus 2.x 的 REST API（/v2/vectordb/entities/*）实现向量检索，
无需在导入期建立 pymilvus 连接，降低依赖耦合。

适用场景：
    - 向量规模 > 500 万（大型企业知识库）；
    - 需要专用向量引擎的高级特性（IVF/PQ 压缩、分区、动态 Schema）；
    - 私有部署场景（独立 Milvus 集群）。

降级策略：
    - search 经 ``@circuit_call`` 熔断器保护，异常向上传播以记录失败；
      熔断 OPEN 后快速拒绝（CircuitBreakerOpenError），由调用方
      （retriever）捕获并降级为空列表；
    - upsert / delete / health_check 遵循优雅降级，不可用时返回 0 / None。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from app.config import get_settings
from app.rag.vector_store.base import VectorStoreBase
from app.utils.circuit_breaker import circuit_call
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.rag.chunker import Chunk

log = get_logger(__name__)

# Milvus collection 名 — 与检索端保持一致
_MILVUS_COLLECTION: str = "document_chunks"
# 请求超时（秒）
_TIMEOUT: float = 10.0


class MilvusVectorStore(VectorStoreBase):
    """Milvus 向量存储 — 可选实现，适合大规模向量场景。

    通过 REST API 操作 Milvus，避免 pymilvus 导入期连接。
    Collection 需在外部预先创建（schema 包含 doc_id / chunk_id /
    content / embedding / kb_id / title_path / content_type / chunk_strategy）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        collection_name: str = _MILVUS_COLLECTION,
    ) -> None:
        from app.utils.retry import build_retry_http_client

        self._http: httpx.AsyncClient = http_client or build_retry_http_client(
            timeout=_TIMEOUT
        )
        self._collection: str = collection_name
        self._available: bool | None = None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        """获取 Milvus REST API 基地址。"""
        settings = get_settings()
        return f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    @circuit_call("vectorstore_milvus")
    async def search(
        self,
        query_vec: list[float],
        kb_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """通过 Milvus REST API 执行向量相似度检索 — 异常向上传播以触发熔断器。"""
        if self._available is False:
            return []

        import time
        t0 = time.monotonic()
        url = f"{self._base_url()}/v2/vectordb/entities/search"
        payload: dict[str, Any] = {
            "collectionName": self._collection,
            "data": [query_vec],
            "limit": top_k,
            "outputFields": [
                "doc_id",
                "chunk_id",
                "content",
                "kb_id",
                "title_path",
                "content_type",
                "chunk_strategy",
            ],
        }
        if kb_ids:
            payload["filter"] = 'kb_id in ["' + '", "'.join(kb_ids) + '"]'

        log.info("vector_store.milvus.search_start", top_k=top_k, has_kb_filter=bool(kb_ids))
        resp = await self._http.post(url, json=payload)
        resp.raise_for_status()
        data: Any = resp.json()
        self._available = True
        results = self._parse_results(data, top_k)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        log.info("vector_store.milvus.search_done", count=len(results), latency_ms=elapsed_ms)
        return results

    @staticmethod
    def _parse_results(data: Any, top_k: int) -> list[dict[str, Any]]:
        """解析 Milvus REST 返回结果为统一格式。"""
        results: list[dict[str, Any]] = []
        rows: list[Any] = []
        if isinstance(data, dict):
            rows = data.get("data", []) or []
        elif isinstance(data, list):
            rows = data

        for row in rows:
            if not isinstance(row, dict):
                continue
            distance = row.get("distance", 0.0)
            # COSINE 相似度直接作为 score
            score = float(distance) if isinstance(distance, (int, float)) else 0.0
            chunk_id = str(row.get("chunk_id") or row.get("id") or "")
            results.append(
                VectorStoreBase._format_result(
                    doc_id=str(row.get("doc_id") or ""),
                    chunk_id=chunk_id,
                    content=str(row.get("content") or row.get("chunk_text") or ""),
                    score=score,
                    kb_id=str(row.get("kb_id") or "") or None,
                    title=row.get("title_path") or None,
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
        """批量写入向量数据到 Milvus collection。

        ``kb_id`` 字段写入文档所属知识库 ID（入参或 chunk 携带），
        与检索端按知识库过滤对齐；历史 bug 曾错误写入 doc_id。
        """
        if not embeddings or not chunks:
            return 0

        if self._available is False:
            return 0

        url = f"{self._base_url()}/v2/vectordb/entities/upsert"
        n = min(len(embeddings), len(chunks))

        # Milvus REST API 接收 data 数组，每项是一条记录
        records: list[dict[str, Any]] = []
        for i in range(n):
            chunk = chunks[i]
            records.append(
                {
                    "doc_id": doc_id,
                    "chunk_id": chunk.id,
                    "content": chunk.content,
                    "embedding": embeddings[i],
                    "kb_id": self._resolve_kb_id(chunk, doc_id, kb_id),
                    "title_path": chunk.title_path,
                    "content_type": chunk.content_type,
                    "chunk_strategy": chunk.chunk_strategy,
                }
            )

        payload: dict[str, Any] = {
            "collectionName": self._collection,
            "data": records,
        }

        try:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            self._available = True
            log.info(
                "vector_store.milvus.upserted",
                doc_id=doc_id,
                count=n,
            )
            return n
        except Exception as exc:
            if self._available is not False:
                log.warning("vector_store.milvus.upsert_failed", error=str(exc))
            self._available = False
            return 0

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, doc_id: str) -> None:
        """按 doc_id 删除所有向量文档。"""
        if self._available is False:
            return

        url = f"{self._base_url()}/v2/vectordb/entities/delete"
        payload: dict[str, Any] = {
            "collectionName": self._collection,
            "filter": f'doc_id == "{doc_id}"',
        }

        try:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            self._available = True
            log.info("vector_store.milvus.deleted", doc_id=doc_id)
        except Exception as exc:
            if self._available is not False:
                log.warning("vector_store.milvus.delete_failed", error=str(exc))
            self._available = False

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """检查 Milvus 服务是否可用。"""
        url = f"{self._base_url()}/v2/vectordb/collections/list"

        try:
            resp = await self._http.post(url, json={})
            resp.raise_for_status()
            self._available = True
            return True
        except Exception as exc:
            log.warning("vector_store.milvus.health_check_failed", error=str(exc))
            self._available = False
            return False

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self._http.aclose()
