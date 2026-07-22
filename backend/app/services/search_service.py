"""
搜索服务 — 单一职责：编排混合检索流程。

遵循单一职责：SearchService 只负责搜索业务编排
（权限过滤 → 检索引擎调用 → 结果格式化），
不直接编写 SQL，也不感知 HTTP 层细节。

遵循开闭原则：通过依赖注入组合 Repository 与 PermissionService，
新增检索策略只需在 _dispatch_search 中追加分支，不修改既有方法。

遵循依赖倒置：检索引擎通过 HybridRetriever 接口调用，
当 RAG 模块尚未就绪时，自动降级为数据库全文检索（ILIKE）。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Document, KnowledgeBase
from app.models.user import KbMember, User
from app.services.permission_service import PermissionService
from app.schemas.search import SearchType
from app.utils.tenant import apply_tenant_filter

logger = logging.getLogger(__name__)


class SearchService:
    """搜索服务 — 封装混合检索（全文 + 向量）的业务编排。

    每个公开方法遵循统一流程：校验权限 → 调用检索引擎 → 返回结果。
    当 RAG 检索引擎不可用时，自动降级为数据库全文检索，
    保证搜索功能始终可用。
    """

    def __init__(
        self, db: AsyncSession, user: User, tenant_id: UUID | None = None
    ) -> None:
        """初始化搜索服务，注入依赖。

        Args:
            db: 异步数据库会话，事务由 get_db_session 统一管理。
            user: 当前已认证用户，用于权限判定与密级过滤。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: User = user
        self._tenant_id = tenant_id
        self.permission: PermissionService = PermissionService(
            db, user, tenant_id=tenant_id
        )

    # ------------------------------------------------------------------
    # 检索引擎加载
    # ------------------------------------------------------------------

    def _get_retriever(self):
        """尝试加载 RAG 混合检索引擎。

        当 ``app.rag.retriever.HybridRetriever`` 可用时返回实例，
        否则返回 None，调用方降级为数据库全文检索。

        Returns:
            HybridRetriever 实例或 None。
        """
        try:
            from app.rag.retriever import HybridRetriever  # type: ignore[import-not-found]

            return HybridRetriever()
        except ImportError:
            logger.debug("RAG 检索引擎未安装，降级为数据库全文检索")
            return None
        except Exception:
            logger.warning("RAG 检索引擎初始化失败，降级为数据库全文检索")
            return None

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        kb_ids: list[UUID] | None = None,
        search_type: SearchType = SearchType.hybrid,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """执行混合检索并返回格式化结果。

        检索流程：
        1. 确定可搜索的知识库范围（基于权限过滤）；
        2. 调用检索引擎（RAG 或数据库全文降级）；
        3. 对结果应用密级过滤；
        4. 分页返回。

        Args:
            query: 搜索关键词。
            kb_ids: 限定搜索的知识库 ID 列表（为空表示搜索全部可访问知识库）。
            search_type: 检索类型 — fulltext / vector / hybrid。
            page: 页码（从 1 开始）。
            page_size: 每页数量。

        Returns:
            包含 results / total / query 的 dict。
        """
        # 确定可访问的知识库范围
        accessible_kb_ids = await self._get_accessible_kb_ids(kb_ids)

        # 尝试 RAG 检索引擎
        retriever = self._get_retriever()
        if retriever is not None:
            try:
                raw_results = await retriever.search(
                    query=query,
                    kb_ids=accessible_kb_ids,
                    search_type=search_type.value,
                    top_k=page * page_size,
                )
                results = self._format_rag_results(raw_results)
            except Exception:
                logger.exception("RAG 检索引擎执行失败，降级为数据库全文检索")
                results = await self._db_fulltext_search(
                    query, accessible_kb_ids
                )
        else:
            # 降级：数据库全文检索
            results = await self._db_fulltext_search(query, accessible_kb_ids)

        # 密级过滤
        allowed = self.permission.allowed_classifications()
        filtered = [
            r for r in results
            if r.get("classification", "public") in allowed
        ]

        # 分页
        total = len(filtered)
        offset = (page - 1) * page_size
        paged = filtered[offset : offset + page_size]

        return {
            "results": paged,
            "total": total,
            "query": query,
        }

    async def suggest(self, query: str, limit: int = 10) -> list[dict]:
        """搜索建议（自动补全）。

        基于文档标题前缀匹配生成建议。

        Args:
            query: 用户已输入的文本。
            limit: 返回建议数量上限。

        Returns:
            建议 dict 列表，每项包含 text / score。
        """
        if not query or len(query) < 2:
            return []

        accessible_kb_ids = await self._get_accessible_kb_ids(None)
        if not accessible_kb_ids:
            return []

        allowed = self.permission.allowed_classifications()
        pattern = f"{query}%"
        stmt = (
            select(Document.title)
            .where(
                Document.deleted_at.is_(None),
                Document.kb_id.in_(accessible_kb_ids),
                Document.classification.in_(allowed),
                or_(
                    Document.title.ilike(pattern),
                    Document.title.ilike(f"%{query}%"),
                ),
            )
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        stmt = stmt.distinct().limit(limit)
        result = await self.db.execute(stmt)
        titles = result.scalars().all()

        return [
            {"text": title, "score": 1.0 if title.lower().startswith(query.lower()) else 0.5}
            for title in titles
        ]

    async def reindex(self, kb_ids: list[UUID] | None = None, force: bool = False) -> str:
        """触发索引重建（异步任务）。

        当 RAG 检索引擎可用时，调用其 reindex 方法；
        否则记录日志并返回占位任务 ID。

        Args:
            kb_ids: 指定重建的知识库 ID（为空表示全部）。
            force: 是否强制全量重建。

        Returns:
            异步任务 ID。
        """
        retriever = self._get_retriever()
        if retriever is not None:
            try:
                task_id = await retriever.reindex(kb_ids=kb_ids, force=force)
                return str(task_id)
            except Exception:
                logger.exception("RAG 索引重建失败")
                raise

        logger.info("RAG 引擎未就绪，索引重建任务跳过（kb_ids=%s, force=%s）", kb_ids, force)
        return "no-op-rag-not-available"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _get_accessible_kb_ids(
        self, kb_ids: list[UUID] | None
    ) -> list[UUID]:
        """获取当前用户可搜索的知识库 ID 列表。

        当 kb_ids 参数指定时，取其与可访问范围的交集；
        当 kb_ids 为空时，返回全部可访问知识库。

        Args:
            kb_ids: 用户指定的知识库 ID 过滤列表。

        Returns:
            可访问的知识库 ID 列表。
        """
        if self.user.role == "admin":
            # admin 可搜索全部知识库
            if kb_ids:
                return kb_ids
            stmt = select(KnowledgeBase.id).where(
                KnowledgeBase.deleted_at.is_(None)
            )
            stmt = apply_tenant_filter(stmt, KnowledgeBase, self._tenant_id)
            result = await self.db.execute(stmt)
            return [row[0] for row in result.all()]

        # 普通用户：所有者或成员
        member_subq = select(KbMember.kb_id).where(
            KbMember.user_id == self.user.id
        )
        member_subq = apply_tenant_filter(member_subq, KbMember, self._tenant_id)
        stmt = (
            select(KnowledgeBase.id)
            .where(
                KnowledgeBase.deleted_at.is_(None),
                or_(
                    KnowledgeBase.owner_id == self.user.id,
                    KnowledgeBase.id.in_(member_subq),
                ),
            )
        )
        stmt = apply_tenant_filter(stmt, KnowledgeBase, self._tenant_id)
        result = await self.db.execute(stmt)
        accessible: set[UUID] = {row[0] for row in result.all()}

        if kb_ids:
            return [kid for kid in kb_ids if kid in accessible]
        return list(accessible)

    async def _db_fulltext_search(
        self, query: str, kb_ids: list[UUID]
    ) -> list[dict]:
        """数据库全文检索降级方案。

        在 title 和 content_text 字段上执行 ILIKE 匹配，
        title 命中得分高于 content_text 命中。

        Args:
            query: 搜索关键词。
            kb_ids: 限定搜索的知识库 ID 列表。

        Returns:
            搜索结果 dict 列表。
        """
        if not kb_ids:
            return []

        pattern = f"%{query}%"
        stmt = (
            select(Document, KnowledgeBase.name.label("kb_name"))
            .join(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
            .where(
                Document.deleted_at.is_(None),
                Document.kb_id.in_(kb_ids),
                or_(
                    Document.title.ilike(pattern),
                    Document.content_text.ilike(pattern),
                ),
            )
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        stmt = stmt.order_by(Document.created_at.desc()).limit(100)
        result = await self.db.execute(stmt)
        rows = result.all()

        results: list[dict] = []
        for row in rows:
            doc = row[0]
            kb_name = row[1]
            # 标题命中得分 0.9，正文命中得分 0.6
            score = 0.9 if query.lower() in doc.title.lower() else 0.6
            snippet = ""
            if doc.content_text:
                idx = doc.content_text.lower().find(query.lower())
                if idx >= 0:
                    start = max(0, idx - 50)
                    end = min(len(doc.content_text), idx + len(query) + 100)
                    snippet = doc.content_text[start:end]
            results.append(
                {
                    "doc_id": doc.id,
                    "title": doc.title,
                    "snippet": snippet,
                    "score": score,
                    "source": doc.doc_type,
                    "kb_name": kb_name,
                    "highlights": None,
                    "classification": doc.classification,
                }
            )
        return results

    def _format_rag_results(self, raw_results: list) -> list[dict]:
        """将 RAG 检索引擎返回的原始结果格式化为统一 dict。

        Args:
            raw_results: RAG 检索引擎返回的原始结果列表。

        Returns:
            格式化后的搜索结果 dict 列表。
        """
        formatted: list[dict] = []
        for item in raw_results:
            if isinstance(item, dict):
                formatted.append(
                    {
                        "doc_id": item.get("doc_id"),
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", item.get("content", "")),
                        "score": float(item.get("score", 0.0)),
                        "source": item.get("source"),
                        "kb_name": item.get("kb_name"),
                        "highlights": item.get("highlights"),
                        "classification": item.get("classification", "public"),
                    }
                )
            else:
                # 兼容对象形式
                formatted.append(
                    {
                        "doc_id": getattr(item, "doc_id", None),
                        "title": getattr(item, "title", ""),
                        "snippet": getattr(item, "snippet", ""),
                        "score": float(getattr(item, "score", 0.0)),
                        "source": getattr(item, "source", None),
                        "kb_name": getattr(item, "kb_name", None),
                        "highlights": getattr(item, "highlights", None),
                        "classification": getattr(item, "classification", "public"),
                    }
                )
        return formatted

    # ------------------------------------------------------------------
    # 跨系统统一搜索（3.13 Glean 模式）
    # ------------------------------------------------------------------

    async def unified_search(
        self,
        query: str,
        sources: list[str] | None = None,
        top_k: int = 20,
    ) -> dict[str, list]:
        """跨系统统一搜索 — 并行检索多源，合并去重。

        搜索源：
        1. 知识库内部检索（Milvus + OpenSearch，已有能力）
        2. 连接器并行检索（OA/ERP/CRM/邮件，新增）
        3. 结果合并 + 按来源标注

        Args:
            query: 搜索关键词。
            sources: 指定搜索源列表，如 ["knowledge_base", "oa", "erp"]。
                    None 表示搜索所有已启用的连接器。
            top_k: 每个源返回的最大结果数。

        Returns:
            {"knowledge_base": [...], "oa": [...], "erp": [...]}
        """
        results: dict[str, list] = {}

        # 1. 知识库内部检索（复用已有 search 方法）
        if not sources or "knowledge_base" in sources:
            kb_results = await self.search(query=query, page=1, page_size=top_k)
            results["knowledge_base"] = kb_results.get("results", [])

        # 2. 连接器并行检索
        from app.connectors.registry import connector_registry

        active_connectors = connector_registry.get_active()
        connector_tasks = []
        active_connector_ids: list[str] = []

        for connector in active_connectors:
            if sources and connector.connector_id not in sources:
                continue
            connector_tasks.append(
                self._search_connector(connector, query, str(self.user.id), top_k)
            )
            active_connector_ids.append(connector.connector_id)

        if connector_tasks:
            connector_results = await asyncio.gather(*connector_tasks, return_exceptions=True)
            for connector_id, result in zip(active_connector_ids, connector_results):
                if isinstance(result, Exception):
                    logger.warning(
                        "search.connector_failed",
                        connector=connector_id,
                        error=str(result),
                    )
                    results[connector_id] = []
                else:
                    results[connector_id] = result
        elif not sources or any(s != "knowledge_base" for s in sources):
            # 有连接器源但都未启用
            for connector in connector_registry.get_all():
                if sources and connector.connector_id in sources:
                    results[connector.connector_id] = []

        return results

    async def _search_connector(
        self,
        connector,
        query: str,
        user_id: str,
        top_k: int,
    ) -> list[dict]:
        """单个连接器检索 + 权限过滤。

        Args:
            connector: 连接器实例。
            query: 搜索关键词。
            user_id: 用户 ID（权限联邦用）。
            top_k: 返回数量上限。

        Returns:
            搜索结果 dict 列表。
        """
        raw_results = await connector.search(query, top_k)

        # 权限联邦：只返回用户有权限的结果
        permitted_ids = await connector.get_permissions(user_id)
        if permitted_ids:
            raw_results = [
                r for r in raw_results
                if r.metadata.get("id", "") in permitted_ids
            ]

        # 转为 dict 格式
        return [
            {
                "source": r.source,
                "source_label": r.source_label,
                "title": r.title,
                "snippet": r.snippet,
                "url": r.url,
                "score": r.score,
                "metadata": r.metadata,
            }
            for r in raw_results
        ]
