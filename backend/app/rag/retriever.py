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

import asyncio
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
        # P2-Step3: 跨模态 Embedder（懒初始化）
        self._multimodal_embedder: Any | None = None

    # ------------------------------------------------------------------
    # 懒初始化
    # ------------------------------------------------------------------

    async def _get_embedder(self) -> EmbeddingProvider | None:
        """懒初始化文本 Embedder — 用于文本查询向量生成。

        C1/C2 fix: 文本查询必须使用与文档索引相同的文本 Embedder，
        不能切换到多模态 Embedder（jina-clip-v2 维度/向量空间不匹配）。
        跨模态图片检索由独立的 _cross_modal_search() 方法处理。
        """
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

        P2-T5: 新增图谱召回第四路 — 通过 EntityRegistry 实体识别 + Neo4j 多跳遍历。
        P2-T6: 检索前用 EntityRegistry 做同义词扩展，增强 BM25 召回。

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
                    "source": "vector" | "fulltext" | "cross_modal" | "graph",
                    "kb_id": str | None,
                    "title": str | None,
                }
        """
        if not query.strip():
            return []

        # P2-T6: 实体识别 + 同义词扩展（零 LLM，增强 BM25 召回）
        expanded_query = query
        graph_entity_names: list[str] = []
        try:
            from app.config import get_settings as _get_settings

            _settings = _get_settings()
            if _settings.ENTITY_REGISTRY_ENABLED:
                from app.ontology.entity_registry import EntityRegistry

                expanded_terms, graph_entity_names = EntityRegistry.expand_query(query)
                if expanded_terms:
                    # 扩展查询用于 BM25 全文检索（OR 语义）
                    expanded_query = f"{query} {' '.join(expanded_terms)}"
        except Exception as exc:
            log.debug("retriever.entity_expand_failed", error=str(exc))

        # 并发执行四路检索 — asyncio.gather 真正并行（原实现为顺序 await，
        # 延迟为四路之和）。各子方法内部已捕获异常返回空列表，单路失败
        # 不影响其他路；gather 层面再以 return_exceptions 兜底防御。
        # C1/C2 fix: 跨模态检索使用独立索引 + 独立 Embedder，与文本检索隔离
        # P2-T5: 图谱召回（第四路）
        (
            vector_results,
            fulltext_results,
            cross_modal_results,
            graph_results,
        ) = await asyncio.gather(
            self._vector_search(query, kb_ids, top_k),
            self._fulltext_search(expanded_query, kb_ids, top_k),
            self._cross_modal_search(query, kb_ids, top_k),
            self._graph_search(graph_entity_names, kb_ids, top_k),
            return_exceptions=True,
        )
        # 兜底净化：子方法理论上已自捕获异常，此处防御未来重构引入的逃逸异常
        vector_results = self._ensure_list(vector_results, "vector")
        fulltext_results = self._ensure_list(fulltext_results, "fulltext")
        cross_modal_results = self._ensure_list(cross_modal_results, "cross_modal")
        graph_results = self._ensure_list(graph_results, "graph")

        merged = self._merge_and_dedupe(
            vector_results + cross_modal_results + graph_results,
            fulltext_results,
            top_k,
        )
        # 父子回溯：将子块内容替换为父块原文，实现「小块检索 → 大块返回」
        merged = await self._expand_to_parents(merged)
        log.info(
            "retriever.search",
            query_len=len(query),
            vector_count=len(vector_results),
            fulltext_count=len(fulltext_results),
            cross_modal_count=len(cross_modal_results),
            graph_count=len(graph_results),
            merged_count=len(merged),
        )
        return merged

    @staticmethod
    def _ensure_list(result: Any, source: str) -> list[dict[str, Any]]:
        """gather(return_exceptions=True) 结果净化 — 异常项降级为空列表。

        四路检索子方法内部已自捕获异常，此方法仅作防御层：
        若未来某路子方法重构后逃逸异常，单路降级为空列表而非拖垮整体检索。
        """
        if isinstance(result, BaseException):
            log.warning("retriever.gather_path_failed", source=source, error=str(result))
            return []
        if not isinstance(result, list):
            log.warning("retriever.gather_path_bad_type", source=source, type=type(result).__name__)
            return []
        return result

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
    # 跨模态检索（独立索引 + jina-clip-v2 Embedder）
    # ------------------------------------------------------------------

    async def _cross_modal_search(
        self,
        query: str,
        kb_ids: list[str] | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """跨模态检索 — 使用 jina-clip-v2 搜索独立图片向量索引。

        C1/C2 fix: 跨模态检索使用独立的索引（ekb_cross_modal）和独立的
        多模态 Embedder（jina-clip-v2, 1024 维），与文本向量检索完全隔离，
        避免维度/向量空间冲突。功能未启用时返回空列表。
        """
        # 懒初始化跨模态 Embedder
        if self._multimodal_embedder is None:
            try:
                from app.llm.multimodal_embedder import get_multimodal_embedder

                self._multimodal_embedder = get_multimodal_embedder()
            except Exception:
                return []
        if self._multimodal_embedder is None:
            return []

        # 生成查询向量（jina-clip-v2 文本嵌入）
        try:
            query_vec = (await self._multimodal_embedder.embed([query]))[0]
        except Exception as exc:
            log.warning("retriever.cross_modal.embed_error", error=str(exc))
            return []

        # 使用独立的跨模态向量存储
        try:
            from app.rag.vector_store.opensearch_store import OpenSearchVectorStore

            cm_store = OpenSearchVectorStore(
                index_name=settings.OPENSEARCH_CROSS_MODAL_INDEX,
                dimension_override=settings.CROSS_MODAL_DIM,
            )
            try:
                results = await cm_store.search(
                    query_vec, kb_ids=kb_ids, top_k=top_k
                )
            finally:
                await cm_store.close()
            # 标记来源为 cross_modal（区别于文本向量检索）
            for r in results:
                r["source"] = "cross_modal"
            return results
        except Exception as exc:
            log.warning("retriever.cross_modal.search_failed", error=str(exc))
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
        # P2-Step1: 修复字段名不匹配 — 写入方使用 content / title_path，
        # 查询方必须使用相同字段名，否则 BM25 查询不存在的字段导致全文检索静默失效。
        query_clause: dict[str, Any] = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["content", "title_path^2"],
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
            "_source": ["doc_id", "chunk_id", "content", "title_path", "kb_id", "content_type", "parent_id"],
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
        """解析 OpenSearch 返回结果为统一格式。

        P2-Step1: 字段名与写入方对齐 — content / title_path / chunk_id。
        """
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
                    "content": str(source.get("content") or source.get("chunk_text") or ""),
                    "score": score,
                    "source": "fulltext",
                    "kb_id": str(source.get("kb_id") or "") or None,
                    "title": source.get("title_path") or source.get("title"),
                    "parent_id": source.get("parent_id") or None,
                }
            )
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------
    # P2-T5: 图谱召回 — 第四路
    # ------------------------------------------------------------------

    async def _graph_search(
        self,
        entity_names: list[str],
        kb_ids: list[str] | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """图谱召回 — 通过实体关系找到关联文档。

        P2-T5: 作为 HybridRetriever 的第四路召回。
        流程：EntityRegistry 识别的实体 → Neo4j 多跳遍历 → 关联 Document 召回。

        优雅降级：Neo4j 不可用或无实体时返回空列表，不影响其他路。

        Args:
            entity_names: EntityRegistry.expand_query() 返回的实体 canonical_name 列表。
            kb_ids: 可选的知识库 ID 过滤。
            top_k: 最大返回结果数。

        Returns:
            候选文档列表，source="graph"。
        """
        if not entity_names:
            return []

        try:
            from app.config import get_settings

            settings = get_settings()
            if not settings.GRAPH_SEARCH_ENABLED:
                return []
        except Exception:
            return []

        try:
            from app.services.graph_service import GraphService

            graph = GraphService()
            related_docs = await graph.find_related_documents_by_entity(
                entity_names=entity_names,
                max_depth=settings.GRAPH_SEARCH_MAX_DEPTH,
                max_results=settings.GRAPH_SEARCH_MAX_RESULTS,
            )
        except Exception as exc:
            log.debug("retriever.graph_search_error", error=str(exc))
            return []

        if not related_docs:
            return []

        # 转换为统一的候选文档格式
        results: list[dict[str, Any]] = []
        kb_id_set = set(kb_ids) if kb_ids else None
        for doc in related_docs:
            doc_id = doc.get("doc_id", "")
            if not doc_id:
                continue
            # kb_ids 权限过滤：图谱节点现携带 kb_id（find_related_documents_by_entity
            # 已返回），限定了知识库范围时，跳过不在授权列表中的文档，
            # 防止其他知识库的文档标题泄漏进生成上下文。
            if kb_id_set is not None and doc.get("kb_id") not in kb_id_set:
                continue

            results.append({
                "doc_id": doc_id,
                "chunk_id": f"graph_{doc_id}",  # 图谱召回无 chunk 级别，用 doc_id 标识
                "content": doc.get("title", ""),  # 图谱召回无内容，用标题占位
                "score": settings.GRAPH_SEARCH_SCORE,  # 固定分，合并时由去重逻辑处理
                "source": "graph",
                "kb_id": doc.get("kb_id"),
                "title": doc.get("title", ""),
            })
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

    # ------------------------------------------------------------------
    # 父子回溯 — 小块检索 → 大块返回
    # ------------------------------------------------------------------

    async def _expand_to_parents(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """父子回溯 — 将子块内容替换为父块原文，并按父块去重。

        核心思路（Small-to-Big Retrieval）：
            1. 向量检索命中的是 256-token 的子块（精确匹配）；
            2. 通过 ``parent_id`` 回溯到父块（整章/整节原文）；
            3. 用父块原文替换子块内容，为 LLM 提供完整上下文；
            4. 同一父块的多个子块去重，只保留最高分的一条。

        去重 key 设计：
            - 有 ``parent_id`` 的子块 → key = parent_id
            - 无 ``parent_id`` 的父块 → key = chunk_id
            这样父块直接命中和子块回溯命中同一父块时自然去重。

        优雅降级：``fetch_by_ids`` 失败时保留子块原内容，不影响检索。
        """
        if not results:
            return results

        # 收集所有 parent_id（去重、过滤空值）
        parent_ids: set[str] = {
            doc["parent_id"]
            for doc in results
            if doc.get("parent_id")
        }

        if not parent_ids:
            # 无父子关系，直接返回（固定长度块或未启用父子索引）
            return results

        # 批量获取父块原文
        parents: dict[str, dict[str, Any]] = {}
        try:
            store = self._get_vector_store()
            parents = await store.fetch_by_ids(list(parent_ids))
        except Exception as exc:
            log.warning("retriever.parent_backtrack_failed", error=str(exc))
            # 降级：保留子块原内容返回
            return results

        if not parents:
            log.debug("retriever.parent_backtrack_empty", parent_count=len(parent_ids))
            return results

        # 扩展 + 去重
        # context_key: parent_id（子块）或 chunk_id（父块/无父子关系块）
        expanded: dict[str, dict[str, Any]] = {}
        backtrack_count = 0
        for doc in results:
            pid = doc.get("parent_id")
            context_key = pid if pid else doc.get("chunk_id")
            if not context_key:
                continue

            doc = dict(doc)  # 浅拷贝，避免修改原列表

            if pid and pid in parents:
                # 用父块原文替换子块内容（保留子块的 score 和 source）
                parent = parents[pid]
                doc["content"] = parent.get("content", doc["content"])
                if parent.get("title_path"):
                    doc["title"] = parent["title_path"]
                doc["expanded_from_child"] = True
                backtrack_count += 1

            # 去重：相同 context_key 保留最高分
            existing = expanded.get(context_key)
            if existing is None or doc["score"] > existing["score"]:
                expanded[context_key] = doc

        ranked = sorted(expanded.values(), key=lambda d: d["score"], reverse=True)

        log.info(
            "retriever.parent_backtrack",
            input_count=len(results),
            parent_ids=len(parent_ids),
            parents_found=len(parents),
            backtrack_count=backtrack_count,
            output_count=len(ranked),
        )
        return ranked

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self._http.aclose()
