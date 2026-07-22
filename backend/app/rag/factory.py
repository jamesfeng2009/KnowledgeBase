"""
RAG 引擎工厂 — 单一职责：按 DEPLOY_MODE 构建并缓存 AgenticRAGEngine 实例。

遵循与 ``app.llm.factory.get_llm_provider`` 相同的懒加载单例模式，
确保全局复用同一个引擎实例（含 LLM / MCP / 检索器 / 重排器 / 生成器）。

P2-5 扩展：``get_rag_engine_by_model(model_id)`` 按 models.json 中的模型 ID
创建对应引擎（复用共享组件，仅替换 LLM / Generator），支持会话级模型切换。

使用方式::

    from app.rag.factory import get_rag_engine, get_rag_engine_by_model

    # 默认引擎（系统默认模型）
    engine = get_rag_engine()
    async for chunk in engine.answer(query, user_id, session_id):
        ...

    # 指定模型引擎（P2 用户级模型选择）
    engine = get_rag_engine_by_model("claude-haiku-4")
    async for chunk in engine.answer(query, user_id, session_id):
        ...
"""

from __future__ import annotations

from functools import lru_cache

from app.rag.engine import AgenticRAGEngine
from app.utils.logger import get_logger

log = get_logger(__name__)

# P2-5: 按 model_id 缓存的引擎实例 — 复用共享组件，仅替换 LLM / Generator
_model_engine_cache: dict[str, AgenticRAGEngine] = {}


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


def get_rag_engine_by_model(model_id: str) -> AgenticRAGEngine:
    """按 model_id 获取 RAG 引擎（P2 用户级模型选择）。

    复用共享组件（MCP Client / Retriever / Reranker），仅替换 LLM Provider
    和 Generator 为指定模型的实例。按 model_id 缓存引擎，避免重复创建。

    与 ``get_rag_engine()`` 的区别：后者返回默认模型引擎（单例），
    本函数返回指定模型的引擎（按 model_id 缓存）。

    Args:
        model_id: models.json 中的模型 ID（如 "claude-haiku-4"）。

    Returns:
        对应模型的 AgenticRAGEngine 实例。

    Raises:
        ValueError: model_id 不存在或不属于当前部署模式。
    """
    # 1. 检查缓存
    if model_id in _model_engine_cache:
        return _model_engine_cache[model_id]

    # 2. 获取模型特定的 LLM Provider
    from app.llm.factory import get_llm_provider_by_model

    llm = get_llm_provider_by_model(model_id)

    # 3. 复用共享组件（从默认引擎获取，避免重复初始化）
    default_engine = get_rag_engine()

    from app.rag.generator import Generator

    generator = Generator(llm)

    # 4. 创建新引擎（共享 MCP / Retriever / Reranker，替换 LLM / Generator）
    engine = AgenticRAGEngine(
        llm=llm,
        mcp_client=default_engine.mcp,
        retriever=default_engine.retriever,
        reranker=default_engine.reranker,
        generator=generator,
        tool_guard=default_engine._tool_guard,
    )

    # 5. 缓存
    _model_engine_cache[model_id] = engine
    log.info("rag_engine.by_model.created", model_id=model_id)

    return engine


def clear_model_engine_cache() -> None:
    """清除按 model_id 缓存的引擎实例 — 供测试使用。"""
    _model_engine_cache.clear()
