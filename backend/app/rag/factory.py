"""
RAG 引擎工厂 — 单一职责：按 DEPLOY_MODE 构建并缓存 AgenticRAGEngine 单例。

遵循与 ``app.llm.factory.get_llm_provider`` 相同的懒加载单例模式，
确保全局复用同一个引擎实例（含 LLM / MCP / 检索器 / 重排器 / 生成器）。

使用方式::

    from app.rag.factory import get_rag_engine

    engine = get_rag_engine()
    async for chunk in engine.answer(query, user_id, session_id):
        ...
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.rag.engine import AgenticRAGEngine
from app.utils.logger import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_rag_engine() -> AgenticRAGEngine:
    """获取全局 RAG 引擎单例（按 DEPLOY_MODE 自动切换底层实现）。

    依赖 ``get_llm_provider()`` / ``HybridRetriever()`` / ``get_reranker()``
    等工厂函数，各组件按部署模式自动选择实现（DashScope / OpenAI / Cohere 等）。

    Returns:
        AgenticRAGEngine 实例（全局复用）。

    Raises:
        RuntimeError: 核心依赖初始化失败。
    """
    from app.database import async_session_factory
    from app.llm.factory import get_llm_provider
    from app.mcp.client import MCPClient
    from app.mcp.server import KnowledgeBaseMCPServer
    from app.rag.generator import Generator
    from app.rag.reranker import get_reranker
    from app.rag.retriever import HybridRetriever

    llm = get_llm_provider()
    mcp = MCPClient(KnowledgeBaseMCPServer(db_factory=async_session_factory))
    retriever = HybridRetriever()
    reranker = get_reranker()
    generator = Generator(llm)

    engine = AgenticRAGEngine(
        llm=llm,
        mcp_client=mcp,
        retriever=retriever,
        reranker=reranker,
        generator=generator,
    )

    log.info("rag_engine.initialized", deploy_mode=getattr(llm, "deploy_mode", "unknown"))
    return engine
