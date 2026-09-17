"""
Qdrant 向量存储 — 单一职责：通过 qdrant-client 实现 VectorStoreBase 契约。

P1 存储抽象层扩展：在既有 os_knn（OpenSearch）与 milvus 之外，
提供 Qdrant 后端（支持超大规模向量、Payload 过滤、HNSW 索引）。

设计要点：
    - 同步 qdrant-client 通过 ``asyncio.to_thread`` 包装，不阻塞事件循环；
    - 首次 upsert 时按 ``dimension`` 自动建 collection（HNSW, Cosine）；
    - payload 字段与 OpenSearch 版对齐（doc_id/chunk_id/content/kb_id/
      title_path/parent_id/updated_at/effective_from/effective_to/doc_role
      /层级字段），检索结果复用 ``_format_result`` 统一格式；
    - 幂等 upsert：以 chunk.id 为 point id，重复写入即覆盖。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.rag.vector_store.base import VectorStoreBase
from app.utils.logger import get_logger

log = get_logger(__name__)

try:  # 依赖可选：未安装 qdrant-client 时 factory 不注册该后端
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        MatchAny,
        PointStruct,
        VectorParams,
    )
except Exception:  # pragma: no cover - 导入失败由 factory 注册期处理
    QdrantClient = None  # type: ignore[assignment,misc]
    Distance = VectorParams = PointStruct = Filter = FieldCondition = MatchAny = None  # type: ignore[assignment,misc]


class QdrantVectorStore(VectorStoreBase):
    """Qdrant 向量存储实现 — 支持 Payload 过滤与多租户 kb_id 隔离。"""

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        collection: str | None = None,
    ) -> None:
        if QdrantClient is None:
            raise RuntimeError("qdrant-client 未安装，无法使用 QdrantVectorStore")
        settings = get_settings()
        self._url: str = url or settings.QDRANT_URL
        self._api_key: str = api_key or settings.QDRANT_API_KEY
        self._collection: str = collection or settings.QDRANT_COLLECTION
        self._client: QdrantClient = QdrantClient(
            url=self._url,
            api_key=self._api_key or None,
        )
        self._ready: bool = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _ensure_collection(self) -> bool:
        """确保 collection 存在 — 不存在则按当前维度创建。"""
        try:
            exists = await asyncio.to_thread(
                self._client.collection_exists, self._collection
            )
            if not exists:
                await asyncio.to_thread(
                    self._client.create_collection,
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=self.dimension, distance=Distance.COSINE
                    ),
                )
                log.info(
                    "vector_store.qdrant.collection_created",
                    collection=self._collection,
                    dim=self.dimension,
                )
            self._ready = True
            return True
        except Exception as exc:
            log.warning("vector_store.qdrant.collection_failed", error=str(exc)[:200])
            return False

    def _payload_for_chunk(
        self,
        doc_id: str,
        chunk: Any,
        kb_id: str | None,
        doc_updated_at: str | None,
        effective_from: str | None,
        effective_to: str | None,
        doc_meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """构造 point payload — 字段对齐 OpenSearch 版，供检索过滤。"""
        payload: dict[str, Any] = {
            "doc_id": doc_id,
            "chunk_id": chunk.id,
            "content": chunk.content,
            "kb_id": self._resolve_kb_id(chunk, doc_id, kb_id),
            "title_path": getattr(chunk, "title_path", None),
            "parent_id": getattr(chunk, "parent_id", None),
        }
        if doc_updated_at is not None:
            payload["updated_at"] = doc_updated_at
        if effective_from is not None:
            payload["effective_from"] = effective_from
        if effective_to is not None:
            payload["effective_to"] = effective_to
        if doc_meta:
            for field in (
                "series_id", "path", "doc_parent_id", "depth", "version_of",
                "doc_status", "classification", "doc_role",
            ):
                val = doc_meta.get(field)
                if val is not None:
                    payload[field] = str(val)
        return payload

    def _build_filter(
        self, kb_ids: list[str] | None, filters: dict[str, Any] | None
    ) -> Filter | None:
        """构造 Qdrant Filter — 支持 kb_ids 与常用层级字段。"""
        conditions: list[FieldCondition] = []
        if kb_ids:
            conditions.append(
                FieldCondition(key="kb_id", match=MatchAny(any=kb_ids))
            )
        if filters:
            for key in (
                "series_id", "doc_parent_id", "version_of", "doc_status",
                "classification", "doc_role",
            ):
                val = filters.get(key)
                if val is not None:
                    conditions.append(
                        FieldCondition(
                            key=key, match=MatchAny(any=[str(val)])
                        )
                    )
            path_prefix = filters.get("path_prefix")
            if path_prefix is not None:
                conditions.append(
                    FieldCondition(
                        key="path", match=MatchAny(any=[str(path_prefix)])
                    )
                )
        if not conditions:
            return None
        return Filter(must=conditions)

    # ------------------------------------------------------------------
    # VectorStoreBase 契约
    # ------------------------------------------------------------------

    async def search(
        self,
        query_vec: list[float],
        kb_ids: list[str] | None = None,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Qdrant 向量相似度检索（HNSW + Cosine）。"""
        if self._client is None:
            return []
        if not self._ready and not await self._ensure_collection():
            return []

        qfilter = self._build_filter(kb_ids, filters)
        try:
            points = await asyncio.to_thread(
                self._client.query_points,
                collection_name=self._collection,
                query=query_vec,
                limit=top_k,
                query_filter=qfilter,
                with_payload=True,
            )
        except Exception as exc:
            log.warning("vector_store.qdrant.search_failed", error=str(exc)[:200])
            return []

        results: list[dict[str, Any]] = []
        for point in points.points:
            payload = point.payload or {}
            results.append(
                self._format_result(
                    doc_id=str(payload.get("doc_id", "")),
                    chunk_id=str(payload.get("chunk_id", point.id)),
                    content=str(payload.get("content", "")),
                    score=float(point.score),
                    kb_id=payload.get("kb_id"),
                    title=payload.get("title_path"),
                    parent_id=payload.get("parent_id"),
                    updated_at=payload.get("updated_at"),
                    effective_from=payload.get("effective_from"),
                    effective_to=payload.get("effective_to"),
                    doc_role=payload.get("doc_role"),
                )
            )
        return results

    async def upsert(
        self,
        doc_id: str,
        chunks: list[Any],
        embeddings: list[list[float]],
        kb_id: str | None = None,
        doc_updated_at: str | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        doc_meta: dict[str, Any] | None = None,
    ) -> int:
        """批量写入向量 — 以 chunk.id 为 point id，幂等覆盖。"""
        if not embeddings or not chunks or self._client is None:
            return 0
        if not self._ready and not await self._ensure_collection():
            return 0

        n = min(len(embeddings), len(chunks))
        points = [
            PointStruct(
                id=chunks[i].id,
                vector=embeddings[i],
                payload=self._payload_for_chunk(
                    doc_id, chunks[i], kb_id, doc_updated_at,
                    effective_from, effective_to, doc_meta,
                ),
            )
            for i in range(n)
        ]
        try:
            await asyncio.to_thread(
                self._client.upsert,
                collection_name=self._collection,
                points=points,
            )
            return n
        except Exception as exc:
            log.warning("vector_store.qdrant.upsert_failed", error=str(exc)[:200])
            return 0

    async def delete(self, doc_id: str) -> None:
        """按 doc_id 删除该文档的全部向量。"""
        if self._client is None:
            return
        try:
            await asyncio.to_thread(
                self._client.delete,
                collection_name=self._collection,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="doc_id", match=MatchAny(any=[str(doc_id)])
                        )
                    ]
                ),
            )
        except Exception as exc:
            log.warning("vector_store.qdrant.delete_failed", error=str(exc)[:200])

    async def fetch_by_ids(
        self, chunk_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """按 chunk_id 批量获取元数据（不含向量）。"""
        if not chunk_ids or self._client is None:
            return {}
        try:
            points = await asyncio.to_thread(
                self._client.retrieve,
                collection_name=self._collection,
                ids=list(chunk_ids),
                with_vectors=False,
                with_payload=True,
            )
        except Exception as exc:
            log.warning("vector_store.qdrant.fetch_failed", error=str(exc)[:200])
            return {}

        out: dict[str, dict[str, Any]] = {}
        for point in points:
            payload = point.payload or {}
            out[str(payload.get("chunk_id", point.id))] = {
                "content": payload.get("content", ""),
                "title_path": payload.get("title_path"),
                "parent_id": payload.get("parent_id"),
                "doc_id": payload.get("doc_id"),
            }
        return out

    async def health_check(self) -> bool:
        """健康检查 — collection 存在且可访问。"""
        if self._client is None:
            return False
        try:
            return bool(
                await asyncio.to_thread(
                    self._client.collection_exists, self._collection
                )
            )
        except Exception:
            return False

    async def close(self) -> None:
        """关闭底层连接（测试与优雅停机用）。"""
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
