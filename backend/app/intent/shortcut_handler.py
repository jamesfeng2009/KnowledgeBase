"""
快捷路径处理器 — 单一职责：确定性检索 + 1 次 LLM 生成。

对于规则匹配到的简单意图（RAG_SEARCH / LIST_DOCUMENTS），跳过 Agent Loop 的
think→retrieve→generate 循环，直接走：检索 → 重排 → 生成（1 次 LLM 调用）。

遵循单一职责：本模块只负责快捷路径的执行编排，
检索/重排/生成分别委托 HybridRetriever / Reranker / Generator。

遵循优雅降级：快捷路径任何步骤失败时，返回 error SSE 事件，
由上层 ChatService 决定是否回退到 Agent Loop。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.intent.router import IntentResult, IntentType
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.sse import SSEEvent, SSEEventType

log = get_logger(__name__)

# 快捷路径检索参数 — 与引擎默认值对齐
_SHORTCUT_TOP_K: int = 20
_SHORTCUT_RERANK_TOP_K: int = 5


class ShortcutHandler:
    """快捷路径处理器 — 确定性检索 + 1 次 LLM 生成。

    使用方式::

        handler = ShortcutHandler()
        async for event in handler.handle(intent, query, user, db, memory_context):
            yield event  # SSEEvent | str
    """

    def __init__(self) -> None:
        """初始化快捷路径处理器。"""
        self._retriever = None
        self._reranker = None
        self._generator = None

    async def handle(
        self,
        intent: IntentResult,
        query: str,
        user: User,
        db: AsyncSession,
        tenant_id: UUID | None = None,
        kb_ids: list[str] | None = None,
        memory_context: str = "",
    ) -> AsyncIterator[SSEEvent | str]:
        """处理快捷路径意图，返回 SSE 流。

        Args:
            intent: 意图路由结果。
            query: 用户原始查询。
            user: 当前用户。
            db: 数据库会话。
            tenant_id: 租户 ID。
            kb_ids: 知识库 ID 列表（可选）。
            memory_context: 记忆上下文。

        Yields:
            SSEEvent | str: SSE 事件和 token 字符串。
        """
        try:
            if intent.intent == IntentType.RAG_SEARCH:
                async for event in self._handle_search(
                    query, user, db, tenant_id, kb_ids, memory_context
                ):
                    yield event
            elif intent.intent == IntentType.LIST_DOCUMENTS:
                async for event in self._handle_list(
                    user, db, tenant_id, memory_context
                ):
                    yield event
            elif intent.intent == IntentType.GET_DOCUMENT:
                # GET_DOCUMENT 需要文档 ID，暂走 Agent Loop
                # 未来可通过 intent.parameters["document_ref"] 查询
                yield SSEEvent(
                    data={"type": "error", "message": "文档详情查询请使用完整搜索"},
                    event=SSEEventType.ERROR,
                )
            else:
                yield SSEEvent(
                    data={"type": "error", "message": f"不支持的快捷意图: {intent.intent.value}"},
                    event=SSEEventType.ERROR,
                )
        except Exception as exc:
            log.error("shortcut_handler.error", error=str(exc), intent=intent.intent.value)
            yield SSEEvent(
                data={"type": "error", "message": f"快捷路径执行失败: {exc}"},
                event=SSEEventType.ERROR,
            )

    async def _handle_search(
        self,
        query: str,
        user: User,
        db: AsyncSession,
        tenant_id: UUID | None,
        kb_ids: list[str] | None,
        memory_context: str,
    ) -> AsyncIterator[SSEEvent | str]:
        """快捷搜索路径 — 检索 → 重排 → 生成（1 次 LLM）。"""

        # 1. 确定性检索（零 LLM）
        retriever = self._get_retriever()
        yield SSEEvent(
            data={"query": query},
            event=SSEEventType.RETRIEVE_START,
        )

        candidates = await retriever.search(query, kb_ids=kb_ids, top_k=_SHORTCUT_TOP_K)

        yield SSEEvent(
            data={"doc_count": len(candidates)},
            event=SSEEventType.RETRIEVE_END,
        )

        if not candidates:
            # 无检索结果 — 直接生成"未找到"回答（1 次 LLM）
            generator = self._get_generator()
            async for token in generator.generate(
                query=query,
                retrieved_docs=[],
                tool_results=[],
                memory_context=memory_context,
            ):
                yield token
            yield SSEEvent(
                data={"sources": [], "shortcut": True},
                event=SSEEventType.SOURCES,
            )
            yield SSEEvent(
                data={"token_count": 0, "shortcut": True, "retrieved_docs": 0},
                event=SSEEventType.DONE,
            )
            return

        # 2. 确定性重排（零 LLM）
        reranker = self._get_reranker()
        try:
            rerank_results = await reranker.rerank(
                query=query,
                documents=candidates,
                top_k=_SHORTCUT_RERANK_TOP_K,
            )
            # 重排契约返回 {index, score, content}（见 RerankerBase），
            # 必须按 index 映射回候选文档，否则 doc_id/title 等元数据丢失。
            reranked = []
            for item in rerank_results:
                idx = item.get("index")
                if isinstance(idx, int) and 0 <= idx < len(candidates):
                    doc = dict(candidates[idx])
                    doc["score"] = item.get("score", doc.get("score", 0.0))
                    reranked.append(doc)
            if not reranked:
                reranked = candidates[:_SHORTCUT_RERANK_TOP_K]
        except Exception as exc:
            log.warning("shortcut.rerank_failed", error=str(exc))
            reranked = candidates[:_SHORTCUT_RERANK_TOP_K]

        # 3. yield sources 事件
        sources = [
            {
                "doc_id": r.get("doc_id", ""),
                "title": r.get("title", ""),
                "score": r.get("score", 0.0),
                "chunk_id": r.get("chunk_id", ""),
            }
            for r in reranked
        ]
        yield SSEEvent(
            data={"sources": sources, "shortcut": True},
            event=SSEEventType.SOURCES,
        )

        # 4. LLM 生成回答（1 次 LLM 调用）
        generator = self._get_generator()
        token_count = 0
        async for token in generator.generate(
            query=query,
            retrieved_docs=reranked,
            tool_results=[],
            memory_context=memory_context,
        ):
            token_count += 1
            yield token

        # 5. yield done 事件
        yield SSEEvent(
            data={
                "token_count": token_count,
                "shortcut": True,
                "retrieved_docs": len(reranked),
            },
            event=SSEEventType.DONE,
        )

    async def _handle_list(
        self,
        user: User,
        db: AsyncSession,
        tenant_id: UUID | None,
        memory_context: str,
    ) -> AsyncIterator[SSEEvent | str]:
        """快捷列表路径 — 查询数据库直接返回（零 LLM）。"""
        from app.repositories.knowledge_repository import DocumentRepository, KnowledgeBaseRepository

        kb_repo = KnowledgeBaseRepository(db, tenant_id=tenant_id)
        doc_repo = DocumentRepository(db, tenant_id=tenant_id)

        # 查询用户可访问的知识库
        kbs = await kb_repo.get_all(limit=20)
        docs = await doc_repo.get_all(limit=20)

        # 格式化为结构化文本（不调 LLM）
        kb_list = [f"- {kb.title} (ID: {kb.id})" for kb in kbs]
        doc_list = [f"- {doc.title} (KB: {doc.kb_id})" for doc in docs]

        response_text = "## 知识库列表\n\n"
        response_text += "\n".join(kb_list) if kb_list else "暂无知识库"
        response_text += "\n\n## 最近文档\n\n"
        response_text += "\n".join(doc_list) if doc_list else "暂无文档"

        yield response_text

        yield SSEEvent(
            data={"sources": [], "shortcut": True},
            event=SSEEventType.SOURCES,
        )
        yield SSEEvent(
            data={"token_count": 0, "shortcut": True, "retrieved_docs": 0},
            event=SSEEventType.DONE,
        )

    # ------------------------------------------------------------------
    # 懒初始化 — 复用工厂单例
    # ------------------------------------------------------------------

    def _get_retriever(self):
        """获取 HybridRetriever 实例（复用引擎单例的检索器）。"""
        if self._retriever is None:
            from app.rag.retriever import HybridRetriever

            self._retriever = HybridRetriever()
        return self._retriever

    def _get_reranker(self):
        """获取 Reranker 实例（复用工厂单例）。"""
        if self._reranker is None:
            from app.rag.reranker import get_reranker

            self._reranker = get_reranker()
        return self._reranker

    def _get_generator(self):
        """获取 Generator 实例（复用 LLM Provider）。"""
        if self._generator is None:
            from app.llm.factory import get_llm_provider
            from app.rag.generator import Generator

            self._generator = Generator(get_llm_provider())
        return self._generator
