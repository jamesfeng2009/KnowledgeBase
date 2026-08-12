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

import re
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

# Milvus filter 表达式值白名单 — kb_id / doc_id 均为 UUID 或内部标识符，
# 只允许字母数字、连字符、下划线。filter 是字符串拼接（REST API 不支持参数化），
# 白名单校验可防止双引号闭合注入（如 `a"] or doc_id != ""` 恒真表达式导致越权检索）。
_FILTER_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_filter_value(value: str) -> str:
    """校验 Milvus filter 表达式中的字符串值，非法值抛 ValueError。"""
    if not value or not _FILTER_VALUE_RE.match(value):
        raise ValueError(f"非法 Milvus filter 值: {value!r}")
    return value


class MilvusVectorStore(VectorStoreBase):
    """Milvus 向量存储 — 可选实现，适合大规模向量场景。

    通过 REST API 操作 Milvus，避免 pymilvus 导入期连接。
    Collection 需在外部预先创建（schema 包含 doc_id / chunk_id /
    content / embedding / kb_id / title_path / content_type /
    chunk_strategy / parent_id）。
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
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """通过 Milvus REST API 执行向量相似度检索 — 异常向上传播以触发熔断器。

        P0 wiki 层级：filters 通过 filter_builder.build_milvus_expr 转为
        Milvus expr 字符串，与 kb_ids 合并。两者都为空时不设 filter。
        """
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
                "parent_id",
                "doc_updated_at",
                "effective_from",
                "effective_to",
                # P0 wiki 层级字段
                "series_id",
                "path",
                "doc_parent_id",
                "depth",
                "version_of",
                # P0-1: 文档状态字段（检索结果可携带，便于上层验证）
                "doc_status",
            ],
        }

        # P0 wiki 层级：合并 kb_ids + filters 为 Milvus expr 字符串
        from app.rag.filter_builder import build_milvus_expr

        # kb_id 仍走 _safe_filter_value 白名单（UUID 安全），
        # filters 中的值由 filter_builder 转义（支持中文路径等）
        expr_parts: list[str] = []
        if kb_ids:
            safe_ids = [_safe_filter_value(k) for k in kb_ids]
            expr_parts.append('kb_id in ["' + '", "'.join(safe_ids) + '"]')
        # filters 由 filter_builder 构建额外 expr 片段
        if filters:
            filters_expr = build_milvus_expr(None, filters)  # kb_ids 已单独处理
            if filters_expr:
                expr_parts.append(filters_expr)
        if expr_parts:
            payload["filter"] = " and ".join(expr_parts)

        log.info(
            "vector_store.milvus.search_start",
            top_k=top_k,
            has_kb_filter=bool(kb_ids),
            has_hierarchy_filter=bool(filters),
        )
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
                    parent_id=row.get("parent_id") or None,
                    updated_at=row.get("doc_updated_at") or None,
                    effective_from=row.get("effective_from") or None,
                    effective_to=row.get("effective_to") or None,
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
        doc_updated_at: str | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        doc_meta: dict[str, Any] | None = None,
    ) -> int:
        """批量写入向量数据到 Milvus collection。

        ``kb_id`` 字段写入文档所属知识库 ID（入参或 chunk 携带），
        与检索端按知识库过滤对齐；历史 bug 曾错误写入 doc_id。
        recency 字段（doc_updated_at / effective_from / effective_to）
        仅在提供时写入，依赖 collection 开启动态 schema。

        P0 wiki 层级：``doc_meta`` 携带文档级层级元数据
        （series_id/path/doc_parent_id/depth/version_of），依赖动态 schema 写入。
        """
        if not embeddings or not chunks:
            return 0

        if self._available is False:
            return 0

        url = f"{self._base_url()}/v2/vectordb/entities/upsert"
        n = min(len(embeddings), len(chunks))

        # P0 wiki 层级：提取文档级层级字段（所有 chunk 共享同一文档的层级元数据）
        hierarchy_fields: dict[str, Any] = {}
        if doc_meta:
            for field in ("series_id", "path", "doc_parent_id", "version_of"):
                val = doc_meta.get(field)
                if val is not None:
                    hierarchy_fields[field] = str(val)
            depth_val = doc_meta.get("depth")
            if depth_val is not None:
                try:
                    hierarchy_fields["depth"] = int(depth_val)
                except (ValueError, TypeError):
                    pass
            # P0-1: 文档状态写入索引，供检索端按 doc_status=published 过滤
            doc_status = doc_meta.get("doc_status")
            if doc_status is not None:
                hierarchy_fields["doc_status"] = str(doc_status)

        # Milvus REST API 接收 data 数组，每项是一条记录
        records: list[dict[str, Any]] = []
        for i in range(n):
            chunk = chunks[i]
            record: dict[str, Any] = {
                "doc_id": doc_id,
                "chunk_id": chunk.id,
                "content": chunk.content,
                "embedding": embeddings[i],
                "kb_id": self._resolve_kb_id(chunk, doc_id, kb_id),
                "title_path": chunk.title_path,
                "content_type": chunk.content_type,
                "chunk_strategy": chunk.chunk_strategy,
                "parent_id": chunk.parent_id or "",
            }
            if doc_updated_at:
                record["doc_updated_at"] = doc_updated_at
            if effective_from:
                record["effective_from"] = effective_from
            if effective_to:
                record["effective_to"] = effective_to
            # P0 wiki 层级字段：所有 chunk 共享文档级层级元数据
            record.update(hierarchy_fields)
            records.append(record)

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
            "filter": f'doc_id == "{_safe_filter_value(doc_id)}"',
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
    # fetch_by_ids — 父子回溯基础
    # ------------------------------------------------------------------

    async def fetch_by_ids(
        self, chunk_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """按 chunk_id 批量获取文档元数据（不含向量）。

        父子索引回溯核心：检索命中子块后，按 ``parent_id`` 批量获取
        父块原文，实现「小块检索 → 大块返回」的上下文扩充。

        使用 Milvus ``query`` API 按 ``chunk_id`` 过滤批量获取。
        """
        if not chunk_ids:
            return {}

        # 白名单校验所有 chunk_id（防注入）
        safe_ids: list[str] = []
        for cid in chunk_ids:
            try:
                safe_ids.append(_safe_filter_value(cid))
            except ValueError:
                log.warning("vector_store.milvus.fetch_by_ids_skip_invalid", chunk_id=cid)

        if not safe_ids:
            return {}

        url = f"{self._base_url()}/v2/vectordb/entities/query"
        filter_expr = 'chunk_id in ["' + '", "'.join(safe_ids) + '"]'
        payload: dict[str, Any] = {
            "collectionName": self._collection,
            "filter": filter_expr,
            "outputFields": [
                "chunk_id",
                "content",
                "title_path",
                "doc_id",
                "content_type",
                "chunk_strategy",
            ],
        }

        try:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("vector_store.milvus.fetch_by_ids_failed", error=str(exc))
            return {}

        data: Any = resp.json()
        result: dict[str, dict[str, Any]] = {}
        rows: list[Any] = []
        if isinstance(data, dict):
            rows = data.get("data", []) or []
        elif isinstance(data, list):
            rows = data

        for row in rows:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("chunk_id") or "")
            if not cid:
                continue
            result[cid] = {
                "content": str(row.get("content") or ""),
                "title_path": str(row.get("title_path") or ""),
                "doc_id": str(row.get("doc_id") or ""),
                "content_type": str(row.get("content_type") or ""),
                "chunk_strategy": str(row.get("chunk_strategy") or ""),
            }
        return result

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
