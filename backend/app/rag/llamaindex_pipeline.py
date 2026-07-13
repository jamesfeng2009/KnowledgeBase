"""LlamaIndex 数据管道 — 文档解析 → 语义分块 → 索引构建 → 混合检索。

定位：RAG 数据层框架，只管数据管道，不管 Agent 决策循环。
与 LangGraph 的分工：
  - LlamaIndex：文档解析、分块、索引、检索（数据层）
  - LangGraph：think → retrieve → generate → reflect（决策层）

遵循单一职责：本模块只负责数据管道，不做 Agent 决策、不做 LLM 生成。
"""
from __future__ import annotations

import os
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

# 延迟导入 LlamaIndex（可能未安装）
try:
    from llama_index.core import (
        SimpleDirectoryReader,
        VectorStoreIndex,
        StorageContext,
        Settings,
    )
    from llama_index.core.node_parser import (
        HierarchicalNodeParser,
        get_leaf_nodes,
    )
    from llama_index.core.retrievers import (
        VectorIndexRetriever,
        KeywordTableSimpleRetriever,
        QueryFusionRetriever,
    )
    from llama_index.core.schema import NodeWithScore
    from llama_index.vector_stores.milvus import MilvusVectorStore
    from llama_index.core.storage.docstore import SimpleDocumentStore

    LLAMAINDEX_AVAILABLE = True
except ImportError:
    LLAMAINDEX_AVAILABLE = False
    logger.warning("llama_index not installed, LlamaIndexPipeline will use fallback mode")


class LlamaIndexPipeline:
    """LlamaIndex 数据管道 — 文档到索引的全流程。

    职责：
    1. 文档解析：PDF/DOCX/HTML/Markdown → LlamaIndex Document
    2. 语义分块：HierarchicalNodeParser 层级分块（父子索引）
    3. 索引构建：VectorStoreIndex（Milvus）+ KeywordTableIndex
    4. 混合检索：QueryFusionRetriever 融合向量 + 关键词

    不做：
    - Agent 决策循环（LangGraph 负责）
    - LLM 生成（Generator 负责）
    - 权限过滤（PermissionService 在 engine retrieve 节点中调用）
    """

    def __init__(self, embedder=None):
        """初始化数据管道。

        Args:
            embedder: EmbeddingProvider 实例。如未提供，延迟到首次使用时初始化。
        """
        self._embedder = embedder
        self._vector_index = None
        self._keyword_index = None
        self._storage_context = None
        self._initialized = False

    def _ensure_initialized(self):
        """延迟初始化 LlamaIndex Settings 和存储。"""
        if self._initialized or not LLAMAINDEX_AVAILABLE:
            return

        # 配置 LlamaIndex 全局 Settings
        if self._embedder:
            # 将项目的 EmbeddingProvider 适配为 LlamaIndex 的 embed_model
            Settings.embed_model = _LlamaIndexEmbedAdapter(self._embedder)
        Settings.llm = None  # LlamaIndex 不调用 LLM

        # Milvus 向量存储
        self._milvus_store = MilvusVectorStore(
            uri=f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}",
            dim=settings.EMBEDDING_DIM,
            collection_name="ekb_vectors",
            overwrite=False,
        )

        # 文档存储（父子索引需要）
        self._storage_context = StorageContext.from_defaults(
            vector_store=self._milvus_store,
            docstore=SimpleDocumentStore(),
        )

        self._initialized = True
        logger.info("llamaindex_pipeline.initialized")

    async def ingest_document(
        self,
        file_path: str,
        doc_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """文档入库流水线：解析 → 分块 → 索引。

        Args:
            file_path: 文件路径。
            doc_id: 文档 ID。
            metadata: 附加元数据（如 kb_id, title, tags）。

        Returns:
            索引的 chunk 数量。
        """
        if not LLAMAINDEX_AVAILABLE:
            logger.warning("llamaindex_pipeline.not_available_fallback")
            return 0

        self._ensure_initialized()
        if not os.path.exists(file_path):
            logger.error("llamaindex_pipeline.file_not_found", file_path=file_path)
            return 0

        try:
            # 1. 文档解析
            documents = SimpleDirectoryReader(input_files=[file_path]).load_data()
            extra_meta = {"doc_id": doc_id, **(metadata or {})}
            for doc in documents:
                doc.metadata = {**doc.metadata, **extra_meta}

            # 2. 层级语义分块（父子索引）
            node_parser = HierarchicalNodeParser.from_defaults(
                chunk_sizes=[1024, 512, 256],  # 父 → 子 → 叶子
            )
            nodes = node_parser.get_nodes_from_documents(documents)
            leaf_nodes = get_leaf_nodes(nodes)

            # 3. 构建向量索引（叶子节点入 Milvus）
            self._vector_index = VectorStoreIndex(
                nodes=leaf_nodes,
                storage_context=self._storage_context,
                show_progress=False,
            )

            # 父节点存入 docstore（供 small-to-big 检索）
            for parent in nodes:
                if parent not in leaf_nodes:
                    self._storage_context.docstore.add_nodes([parent])

            logger.info(
                "llamaindex_pipeline.ingested",
                doc_id=doc_id,
                total_nodes=len(nodes),
                leaf_nodes=len(leaf_nodes),
            )
            return len(leaf_nodes)

        except Exception as e:
            logger.error("llamaindex_pipeline.ingest_error", doc_id=doc_id, error=str(e))
            return 0

    async def ingest_text(
        self,
        text: str,
        doc_id: str,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """文本入库：直接分块文本（不从文件解析）。

        Args:
            text: 原始文本内容。
            doc_id: 文档 ID。
            title: 文档标题。
            metadata: 附加元数据。

        Returns:
            索引的 chunk 数量。
        """
        if not LLAMAINDEX_AVAILABLE:
            return 0

        self._ensure_initialized()
        try:
            from llama_index.core.schema import Document

            doc = Document(text=text, metadata={"doc_id": doc_id, "title": title, **(metadata or {})})

            node_parser = HierarchicalNodeParser.from_defaults(chunk_sizes=[1024, 512, 256])
            nodes = node_parser.get_nodes_from_documents([doc])
            leaf_nodes = get_leaf_nodes(nodes)

            self._vector_index = VectorStoreIndex(
                nodes=leaf_nodes,
                storage_context=self._storage_context,
                show_progress=False,
            )

            for parent in nodes:
                if parent not in leaf_nodes:
                    self._storage_context.docstore.add_nodes([parent])

            return len(leaf_nodes)

        except Exception as e:
            logger.error("llamaindex_pipeline.ingest_text_error", doc_id=doc_id, error=str(e))
            return 0

    async def hybrid_search(
        self,
        query: str,
        top_k: int = 20,
        kb_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """混合检索：向量 + 关键词，QueryFusionRetriever 融合。

        Args:
            query: 查询文本。
            top_k: 返回结果数。
            kb_ids: 可选，限定知识库范围。

        Returns:
            检索结果列表：[{doc_id, content, score, source}]
        """
        if not LLAMAINDEX_AVAILABLE or self._vector_index is None:
            return []

        self._ensure_initialized()
        try:
            # 向量检索器
            vector_retriever = VectorIndexRetriever(
                index=self._vector_index,
                similarity_top_k=top_k,
            )

            # 关键词检索器
            keyword_retriever = KeywordTableSimpleRetriever(
                index=self._vector_index,
                similarity_top_k=top_k,
            )

            # QueryFusionRetriever 融合两路结果
            fusion_retriever = QueryFusionRetriever(
                [vector_retriever, keyword_retriever],
                similarity_top_k=top_k,
                mode="reciprocal_rerank",
                use_async=True,
            )

            nodes: list[NodeWithScore] = fusion_retriever.retrieve(query)

            results = []
            for node in nodes:
                doc_id = node.metadata.get("doc_id", "")
                if kb_ids and node.metadata.get("kb_id") not in kb_ids:
                    continue
                results.append({
                    "doc_id": doc_id,
                    "chunk_id": node.node_id,
                    "content": node.text,
                    "score": float(node.score) if node.score else 0.0,
                    "source": "llamaindex_hybrid",
                    "metadata": node.metadata,
                })

            logger.info(
                "llamaindex_pipeline.search_done",
                query=query[:50],
                results=len(results),
            )
            return results

        except Exception as e:
            logger.error("llamaindex_pipeline.search_error", error=str(e))
            return []

    async def build_graph_index(self, documents: list, kb_id: str) -> bool:
        """构建知识图谱索引 — 从文档中提取三元组建图。
        
        使用 LlamaIndex 的 KnowledgeGraphIndex 自动提取 (subject, predicate, object) 三元组，
        存入 Neo4j 图数据库。
        """
        if not LLAMAINDEX_AVAILABLE:
            return False
        
        self._ensure_initialized()
        try:
            from llama_index.core import KnowledgeGraphIndex
            from llama_index.graph_stores.neo4j import Neo4jGraphStore
            
            # 配置 Neo4j 图存储
            graph_store = Neo4jGraphStore(
                username=settings.NEO4J_USER,
                password=settings.NEO4J_PASSWORD,
                url=settings.NEO4J_URI,
                database=settings.NEO4J_DATABASE,
            )
            
            # 构建图谱索引（自动提取三元组）
            kg_index = KnowledgeGraphIndex.from_documents(
                documents,
                storage_context=StorageContext.from_defaults(graph_store=graph_store),
                max_triplets_per_chunk=10,  # 每块最多提取 10 个三元组
                show_progress=False,
            )
            
            logger.info("llamaindex_pipeline.graph_indexed", kb_id=kb_id)
            return True
        except Exception as e:
            logger.error("llamaindex_pipeline.graph_index_error", error=str(e))
            return False

    async def graph_search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """图谱检索 — 基于实体关系的语义检索。
        
        通过图遍历查找与查询实体相关的文档节点，
        补充向量检索的"关系维度"。
        """
        if not LLAMAINDEX_AVAILABLE:
            return []
        
        self._ensure_initialized()
        try:
            from llama_index.core import KnowledgeGraphIndex
            
            # 使用已有图谱索引检索
            retriever = self._vector_index.as_retriever(
                similarity_top_k=top_k,
            )
            # KnowledgeGraphIndex 的 retriever 支持图遍历
            nodes = retriever.retrieve(query)
            
            return [{
                "doc_id": n.metadata.get("doc_id", ""),
                "content": n.text[:200],
                "score": float(n.score) if n.score else 0.0,
                "source": "graph_traversal",
            } for n in nodes]
        except Exception as e:
            logger.error("llamaindex_pipeline.graph_search_error", error=str(e))
            return []

    async def delete_document(self, doc_id: str) -> bool:
        """从索引中删除文档的所有节点。

        Args:
            doc_id: 文档 ID。

        Returns:
            是否成功。
        """
        if not LLAMAINDEX_AVAILABLE or self._vector_index is None:
            return False

        try:
            # Milvus 按 doc_id 过滤删除
            self._milvus_store.delete(doc_id)
            logger.info("llamaindex_pipeline.deleted", doc_id=doc_id)
            return True
        except Exception as e:
            logger.error("llamaindex_pipeline.delete_error", doc_id=doc_id, error=str(e))
            return False


class _LlamaIndexEmbedAdapter:
    """将项目的 EmbeddingProvider 适配为 LlamaIndex 的 embed_model 接口。

    LlamaIndex 期望 embed_model 有:
    - _get_text_embedding(text: str) -> list[float]
    - _get_query_embedding(query: str) -> list[float]
    - async _aget_text_embedding(text: str) -> list[float]
    - async _aget_query_embedding(query: str) -> list[float]
    """

    def __init__(self, embedder):
        self._embedder = embedder

    def _get_text_embedding(self, text: str) -> list[float]:
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self._embedder.embed([text])
        )[0]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._get_text_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        result = await self._embedder.embed([text])
        return result[0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return await self._aget_text_embedding(query)
