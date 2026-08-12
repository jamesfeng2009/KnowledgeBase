"""
快捷路径处理器 — 单一职责：确定性检索 + 1 次 LLM 生成，并承载终态出口。

对于规则匹配到的简单意图（RAG_SEARCH / LIST_DOCUMENTS），跳过 Agent Loop 的
think→retrieve→generate 循环，直接走：检索 → 硬约束过滤 → 权限过滤 → 重排 → 生成。

同时承载方案二、方案三的终态出口：
- 拒识（UNSUPPORTED）：超出知识库范围，直接返回 INTENT_REJECTED 事件；
- 澄清（UNCLEAR）：参数缺失，返回 CLARIFICATION_REQUIRED 事件，不瞎猜；
- 硬约束（classification_max / exclude_classifications / kb_ids /
  mandatory_keywords）在检索后、权限过滤前强制执行；
- 软约束作为生成提示偏好注入生成上下文。

遵循单一职责：本模块只负责快捷路径与终态出口的执行编排，
检索/重排/生成分别委托 HybridRetriever / Reranker / Generator。

遵循优雅降级：快捷路径任何步骤失败时，返回 error SSE 事件，
由上层 ChatService 决定是否回退到 Agent Loop。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.intent.router import (
    IntentConstraints,
    IntentResult,
    IntentType,
    SlotName,
)
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.sse import SSEEvent, SSEEventType

log = get_logger(__name__)

# 快捷路径检索参数 — 与引擎默认值对齐
_SHORTCUT_TOP_K: int = 20
_SHORTCUT_RERANK_TOP_K: int = 5

# 密级权重表 — 与 permission_service._CLEARANCE_ORDER 口径一致。
# 硬约束（classification_max / exclude_classifications）据此过滤候选文档。
_CLASSIFICATION_WEIGHT: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "secret": 3,
}

# 拒识通用话术
_UNSUPPORTED_MESSAGE = "该问题超出企业知识库服务范围，无法在知识库中作答，请联系对应业务服务台。"

# 澄清缺槽提示文案（槽位名 → 提示语）
_SLOT_HINTS: dict[str, str] = {
    SlotName.SEARCH_QUERY: "请说明您想检索的主题或内容",
    SlotName.TIME_RANGE: "请说明需要的时间范围（如近半年）",
    SlotName.CLASSIFICATION: "请说明需要的文档密级（公开/内部/机密/秘密）",
    SlotName.DOC_TYPE: "请说明需要的文档类型（如规范/手册/报告）",
    SlotName.KB: "请说明要检索的知识库",
}


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
        permission_filter: Any = None,
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
            permission_filter: 可选，请求级 ABAC 权限过滤器（密级维度，
                在重排前应用）。签名为 ``async (list[dict]) -> list[dict]``。

        Yields:
            SSEEvent | str: SSE 事件和 token 字符串。
        """
        try:
            if intent.intent == IntentType.RAG_SEARCH:
                async for event in self._handle_search(
                    query, user, db, tenant_id, kb_ids, memory_context,
                    permission_filter, intent.constraints,
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
            elif intent.intent == IntentType.UNSUPPORTED:
                # 方案三：拒识出口 — 超出知识库范围，直接拒绝，不进检索/Agent Loop
                async for event in self._handle_unsupported():
                    yield event
            elif intent.intent == IntentType.UNCLEAR:
                # 方案三：澄清出口 — 参数缺失，先澄清再回答，不瞎猜
                async for event in self._handle_clarify(
                    intent.missing_slots, intent.parameters
                ):
                    yield event
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
        permission_filter: Any = None,
        constraints: IntentConstraints | None = None,
    ) -> AsyncIterator[SSEEvent | str]:
        """快捷搜索路径 — 检索 → 硬约束过滤 → 权限过滤 → 重排 → 生成（1 次 LLM）。

        约束执行顺序（方案二）：
            用户显式硬约束（密级上限/排除密级/限定知识库/必含关键词）最先过滤，
            随后叠加 ABAC 权限过滤（清密级），二者构成双重保险。
        """

        # 1. 确定性检索（零 LLM）
        retriever = self._get_retriever()
        yield SSEEvent(
            data={"query": query},
            event=SSEEventType.RETRIEVE_START,
        )

        candidates = await retriever.search(query, kb_ids=kb_ids, top_k=_SHORTCUT_TOP_K)

        # 1.2 硬约束过滤（用户显式约束 — 必须在权限过滤前）
        if constraints is not None and candidates:
            try:
                candidates = await self._apply_hard_constraints(
                    candidates, constraints, db, tenant_id
                )
                log.info(
                    "shortcut.hard_constraint_filtered",
                    after=len(candidates),
                )
            except Exception as exc:
                log.error("shortcut.hard_constraint_error", error=str(exc))
                candidates = []

        # 1.5 ABAC 权限过滤（必须在重排之前！）— 与引擎 _retrieve 同一约束。
        # kb_ids 下推只解决"知识库归属"维度，此处补"文档密级"维度；
        # 过滤失败时保守返回空，避免越权文档进入生成上下文。
        if permission_filter is not None and candidates:
            try:
                candidates = await permission_filter(candidates)
                log.info(
                    "shortcut.permission_filtered",
                    after=len(candidates),
                )
            except Exception as exc:
                log.error("shortcut.permission_error", error=str(exc))
                candidates = []

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

        # 3.5 软约束作为生成提示偏好注入记忆上下文（不改 Generator 契约）
        gen_memory = memory_context
        if constraints is not None and constraints.soft:
            hint = self._soft_constraint_hint(constraints.soft)
            if hint:
                gen_memory = f"{memory_context}\n[用户检索偏好]\n{hint}"

        # 4. LLM 生成回答（1 次 LLM 调用）
        generator = self._get_generator()
        token_count = 0
        async for token in generator.generate(
            query=query,
            retrieved_docs=reranked,
            tool_results=[],
            memory_context=gen_memory,
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
        kb_list = [f"- {kb.name} (ID: {kb.id})" for kb in kbs]
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
    # 方案三：拒识 + 澄清出口
    # ------------------------------------------------------------------

    async def _handle_unsupported(self) -> AsyncIterator[SSEEvent | str]:
        """拒识出口 — 用户问题超出知识库服务范围，直接拒绝。

        与生成层拒识不同，此处是路由层决策：明确识别为越界问题，
        不进入检索，也不调用 Agent Loop，避免越权/无关回答。
        """
        yield SSEEvent(
            data={
                "intent": IntentType.UNSUPPORTED.value,
                "message": _UNSUPPORTED_MESSAGE,
            },
            event=SSEEventType.INTENT_REJECTED,
        )
        yield SSEEvent(data={"status": "done"}, event=SSEEventType.DONE)

    async def _handle_clarify(
        self,
        missing_slots: list[str],
        parameters: dict[str, Any],
    ) -> AsyncIterator[SSEEvent | str]:
        """澄清出口 — 参数缺失/歧义，先澄清再回答，不瞎猜。

        按缺槽位名生成可读提示；缺槽为空时回退到通用澄清话术。
        """
        slots = missing_slots or [SlotName.SEARCH_QUERY]
        # 去重并保留顺序
        seen: set[str] = set()
        unique_slots: list[str] = []
        for slot in slots:
            if slot in seen:
                continue
            seen.add(slot)
            unique_slots.append(slot)

        messages: list[str] = []
        for slot in unique_slots:
            hint = _SLOT_HINTS.get(slot)
            if hint:
                messages.append(hint)
        message = "；".join(messages) if messages else "您的问题信息不足，请补充检索主题或范围。"

        yield SSEEvent(
            data={
                "intent": IntentType.UNCLEAR.value,
                "missing_slots": unique_slots,
                "message": message,
            },
            event=SSEEventType.CLARIFICATION_REQUIRED,
        )
        yield SSEEvent(data={"status": "done"}, event=SSEEventType.DONE)

    # ------------------------------------------------------------------
    # 方案二：硬/软约束处理
    # ------------------------------------------------------------------

    async def _apply_hard_constraints(
        self,
        candidates: list[dict],
        constraints: IntentConstraints,
        db: AsyncSession,
        tenant_id: UUID | None,
    ) -> list[dict]:
        """按硬约束过滤候选文档（在权限过滤之前执行）。

        硬约束维度（均为用户显式表达，必须满足）：
            - kb_ids: 限定知识库归属
            - classification_max / exclude_classifications: 密级限制
            - mandatory_keywords: 必含关键词

        任一约束无法满足都保守剔除（fail-closed），防止越权/无关结果进入生成。
        """
        if not candidates:
            return candidates
        hard = constraints.hard or {}
        result = candidates

        # 1. 限定知识库
        kb_ids = hard.get("kb_ids")
        if kb_ids:
            kb_set = {str(k) for k in kb_ids}
            result = [
                c for c in result
                if c.get("kb_id") and str(c["kb_id"]) in kb_set
            ]
            if not result:
                return []

        # 2. 密级限制（classification_max / exclude_classifications）
        c_max = hard.get("classification_max")
        excludes = hard.get("exclude_classifications")
        if c_max or excludes:
            max_weight = _CLASSIFICATION_WEIGHT.get(c_max, 99) if c_max else 99
            exclude_set = {str(e) for e in (excludes or [])}
            # 批量查询密级，避免逐条查库
            doc_ids = {c.get("doc_id") for c in result if c.get("doc_id")}
            cls_map = await self._fetch_classifications(db, tenant_id, doc_ids)
            filtered: list[dict] = []
            for c in result:
                cls = cls_map.get(str(c.get("doc_id")))
                # fail-closed：查不到密级（文档已删/非法 id）保守剔除
                if cls is None:
                    continue
                weight = _CLASSIFICATION_WEIGHT.get(cls, 1)
                if weight > max_weight:
                    continue
                if cls in exclude_set:
                    continue
                filtered.append(c)
            result = filtered
            if not result:
                return []

        # 3. 必含关键词
        mandatory = hard.get("mandatory_keywords")
        if mandatory:
            kws = [str(k).lower() for k in mandatory if k]
            result = [
                c for c in result
                if all(
                    kw in (c.get("content") or "").lower()
                    for kw in kws
                )
            ]
        return result

    async def _fetch_classifications(
        self,
        db: AsyncSession,
        tenant_id: UUID | None,
        doc_ids: set[str],
    ) -> dict[str, str]:
        """批量查询文档密级 — 返回 {str(doc_id): classification}。"""
        if not doc_ids:
            return {}
        from uuid import UUID as _UUID

        from sqlalchemy import select

        from app.models.knowledge import Document
        from app.utils.tenant import apply_tenant_filter

        valid_ids: list[_UUID] = []
        for raw in doc_ids:
            try:
                valid_ids.append(_UUID(str(raw)))
            except (ValueError, TypeError):
                continue
        if not valid_ids:
            return {}
        stmt = select(Document.id, Document.classification).where(
            Document.id.in_(valid_ids)
        )
        stmt = apply_tenant_filter(stmt, Document, tenant_id)
        rows = (await db.execute(stmt)).all()
        return {str(row[0]): row[1] for row in rows}

    @staticmethod
    def _soft_constraint_hint(soft: dict[str, Any]) -> str:
        """将软约束序列化为生成提示文本（作为偏好，非强制）。"""
        parts: list[str] = []
        if soft.get("time_range"):
            parts.append(f"时间范围偏好：{soft['time_range']}")
        if soft.get("doc_type"):
            parts.append(f"文档类型偏好：{soft['doc_type']}")
        if soft.get("source"):
            parts.append(f"来源偏好：{soft['source']}")
        return "；".join(parts)

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
