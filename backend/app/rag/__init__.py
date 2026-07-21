"""
RAG 引擎层 — 检索增强生成的完整流水线。

对外暴露 Agentic RAG 的全部组件，业务层（API / Service）仅依赖本包导出的
抽象入口，按 DEPLOY_MODE 自动切换底层检索 / 重排 / 生成实现。

组件清单：
    - SemanticChunker    — 语义分块器（四级优先级策略）
    - TokenCache         — 三级 Token 缓存（L1 Redis / L2 语义 / L3 Provider 原生）
    - HybridRetriever    — 混合检索器（向量 + 全文）
    - RerankerBase / get_reranker — 双模式重排器（Cohere / TEI）
    - CitationExtractor  — 引用标注提取器
    - Generator          — 答案生成器（流式）
    - AgenticRAGEngine   — Agentic RAG 主引擎（Agent Loop，可选 LangGraph）
    - LlamaIndexPipeline — LlamaIndex 数据管道（文档解析 → 分块 → 索引 → 混合检索）

典型用法::

    from app.rag import AgenticRAGEngine, HybridRetriever, get_reranker, Generator

    engine = AgenticRAGEngine(
        llm=get_llm_provider(),
        mcp_client=mcp_client,
        retriever=HybridRetriever(),
        reranker=get_reranker(),
        generator=Generator(get_llm_provider()),
        permission_filter=my_filter,
    )
    async for chunk in engine.answer(query, user_id, session_id):
        yield chunk  # SSEEvent（thinking/retrieve/tool_call/...）或 str token

LangGraph 可选路径（安装 langgraph 后）::

    async for token in engine.answer_with_graph(query, user_id, session_id):
        yield token  # 支持断点恢复

LlamaIndex 数据管道（安装 llama_index 后）::

    from app.rag import LlamaIndexPipeline
    pipeline = LlamaIndexPipeline(embedder=get_embedder())
    await pipeline.ingest_document(file_path, doc_id, metadata={"kb_id": kb_id})
    results = await pipeline.hybrid_search(query, top_k=20)
"""

from __future__ import annotations

from app.rag.cache import TokenCache
from app.rag.chunker import Chunk, SemanticChunker
from app.rag.citation import CitationExtractor
from app.rag.engine import AgentState, AgenticRAGEngine, PermissionFilter
from app.rag.generator import Generator
from app.rag.llamaindex_pipeline import LlamaIndexPipeline
from app.rag.reranker import (
    CohereReranker,
    RerankerBase,
    TEIReranker,
    get_reranker,
)
from app.rag.retriever import HybridRetriever

__all__ = [
    # 分块
    "Chunk",
    "SemanticChunker",
    # 缓存
    "TokenCache",
    # 检索
    "HybridRetriever",
    # 重排
    "RerankerBase",
    "CohereReranker",
    "TEIReranker",
    "get_reranker",
    # 引用
    "CitationExtractor",
    # 生成
    "Generator",
    # 引擎
    "AgentState",
    "AgenticRAGEngine",
    "PermissionFilter",
    # 数据管道（LlamaIndex）
    "LlamaIndexPipeline",
]
