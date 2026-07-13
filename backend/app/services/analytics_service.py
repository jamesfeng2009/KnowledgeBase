"""
知识健康度分析服务 — 单一职责：为管理员仪表盘提供运营指标数据。

六项运营指标：
    get_search_hotwords     — 搜索热词 Top N
    get_zero_click_queries  — 零点击搜索词
    get_popular_documents   — 文档热度排行
    get_knowledge_coverage  — 知识覆盖率
    get_knowledge_freshness — 知识新鲜度（P1 解耦：knowledge_graph 关闭时降级为 PG 查询）
    get_top_contributors    — 专家贡献排行
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import Integer, cast, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.action import DocumentAction
from app.models.analytics import SearchLog
from app.models.comment import DocumentComment
from app.models.knowledge import Document
from app.models.qa import QaAnswer
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: 文档过期阈值（天）— 超过此天数未更新视为过期
FRESHNESS_EXPIRE_DAYS: int = 180
#: 文档即将过期阈值（天）— 超过此天数未更新视为即将过期
FRESHNESS_EXPIRING_SOON_DAYS: int = 150


class AnalyticsService:
    """知识健康度分析服务 — 为管理员仪表盘提供数据。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 汇总接口
    # ------------------------------------------------------------------

    async def get_dashboard(self, days: int = 30) -> dict:
        """仪表盘汇总 — 一次返回所有指标。

        Args:
            days: 统计周期（天）。

        Returns:
            所有仪表盘指标。
        """
        import asyncio

        results = await asyncio.gather(
            self.get_search_hotwords(days),
            self.get_zero_click_queries(days),
            self.get_popular_documents(days),
            self.get_knowledge_coverage(),
            self.get_knowledge_freshness(),
            self.get_top_contributors(days),
            return_exceptions=True,
        )
        keys = [
            "search_hotwords", "zero_click_queries", "popular_documents",
            "knowledge_coverage", "knowledge_freshness", "top_contributors",
        ]
        dashboard = {}
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                logger.warning("analytics.dashboard_error", metric=key, error=str(result))
                dashboard[key] = []
            else:
                dashboard[key] = result
        return dashboard

    # ------------------------------------------------------------------
    # 单项指标
    # ------------------------------------------------------------------

    async def get_search_hotwords(
        self, days: int = 30, top_k: int = 20,
    ) -> list[dict]:
        """搜索热词 Top N — 按搜索次数降序。

        Args:
            days: 统计周期（天）。
            top_k: 返回数量上限。

        Returns:
            [{"keyword": "报销", "count": 42}, ...]
        """
        since = datetime.utcnow() - timedelta(days=days)
        result = await self.db.execute(
            select(
                SearchLog.query,
                func.count(SearchLog.id).label("search_count"),
            )
            .where(SearchLog.created_at >= since)
            .group_by(SearchLog.query)
            .order_by(desc("search_count"))
            .limit(top_k)
        )
        return [
            {"keyword": r.query, "count": r.search_count}
            for r in result
        ]

    async def get_zero_click_queries(
        self, days: int = 30, top_k: int = 20,
    ) -> list[dict]:
        """零点击搜索词 — 无结果或用户未点击的查询。

        用于知识缺口分析，补充 gap_detector_service 的数据。

        Args:
            days: 统计周期（天）。
            top_k: 返回数量上限。

        Returns:
            [{"keyword": "报销", "count": 5}, ...]
        """
        since = datetime.utcnow() - timedelta(days=days)
        result = await self.db.execute(
            select(
                SearchLog.query,
                func.count(SearchLog.id).label("count"),
            )
            .where(
                SearchLog.created_at >= since,
                SearchLog.clicked.is_(False),
            )
            .group_by(SearchLog.query)
            .order_by(desc("count"))
            .limit(top_k)
        )
        return [
            {"keyword": r.query, "count": r.count}
            for r in result
        ]

    async def get_popular_documents(
        self, days: int = 30, top_k: int = 10,
    ) -> list[dict]:
        """文档热度排行 — 按浏览量降序。

        Args:
            days: 统计周期（天，目前按总浏览量排序）。
            top_k: 返回数量上限。

        Returns:
            [{"id": "...", "title": "...", "views": 123}, ...]
        """
        result = await self.db.execute(
            select(Document.id, Document.title, Document.view_count)
            .where(Document.deleted_at.is_(None))
            .order_by(desc(Document.view_count))
            .limit(top_k)
        )
        return [
            {"id": str(r.id), "title": r.title, "views": r.view_count}
            for r in result
        ]

    async def get_knowledge_coverage(self) -> dict:
        """知识覆盖率 — 已覆盖主题 / 搜索主题比例。

        覆盖主题 = 文档分类去重后的数量
        搜索主题 = 近 30 天热词去重后的数量

        Returns:
            {"covered_topics": 15, "searched_topics": 20, "coverage_ratio": 0.75}
        """
        # 已覆盖主题（文档分类去重）
        covered_result = await self.db.execute(
            select(func.count(func.distinct(Document.category)))
            .where(
                Document.deleted_at.is_(None),
                Document.category.isnot(None),
            )
        )
        covered = covered_result.scalar() or 0

        # 搜索主题（近 30 天热词去重）
        since = datetime.utcnow() - timedelta(days=30)
        searched_result = await self.db.execute(
            select(func.count(func.distinct(SearchLog.query)))
            .where(SearchLog.created_at >= since)
        )
        searched = searched_result.scalar() or 0

        ratio = covered / searched if searched > 0 else 0
        return {
            "covered_topics": covered,
            "searched_topics": searched,
            "coverage_ratio": round(ratio, 2),
        }

    async def get_knowledge_freshness(
        self, tenant_id: uuid.UUID | None = None,
    ) -> dict:
        """知识新鲜度 — 过期/即将过期文档比例。

        P1 解耦：检查 knowledge_graph 模块是否启用。
        - 启用：使用 GraphitiManager 时序图谱检测过期实体
        - 未启用：降级为 PostgreSQL 查询 Document.updated_at

        Args:
            tenant_id: 租户 ID（用于模块门控检查，None 时取第一条活跃租户）。

        Returns:
            {"total_documents": 100, "expired": 5, "expiring_soon": 10, "freshness_rate": 0.95}
        """
        # 检查 knowledge_graph 模块是否启用
        from app.services.tenant_service import TenantService

        tenant_service = TenantService(self.db)
        graph_enabled = await tenant_service.is_module_enabled(
            "knowledge_graph", tenant_id
        )

        if not graph_enabled:
            # 降级：纯 PG 查询文档更新时间
            return await self._freshness_from_pg()

        # 正常路径：GraphitiManager 时序图谱
        from app.memory.graphiti_manager import GraphitiManager

        graphiti = GraphitiManager(self.db)
        try:
            expired = await graphiti.get_expired_entities()
            expiring_soon = await graphiti.get_expiring_soon(days=30)
        except Exception as exc:
            logger.warning("analytics.freshness_error", error=str(exc))
            return await self._freshness_from_pg()

        total_result = await self.db.execute(
            select(func.count(Document.id)).where(Document.deleted_at.is_(None))
        )
        total_docs = total_result.scalar() or 1

        return {
            "total_documents": total_docs,
            "expired": len(expired),
            "expiring_soon": len(expiring_soon),
            "freshness_rate": round((total_docs - len(expired)) / total_docs, 2),
            "source": "graphiti",
        }

    async def _freshness_from_pg(self) -> dict:
        """PG 降级新鲜度检测 — 基于文档 updated_at 时间戳。

        当 knowledge_graph 模块未启用或 GraphitiManager 不可用时调用。
        判定规则：
        - 超过 FRESHNESS_EXPIRE_DAYS 天未更新 → 过期
        - 超过 FRESHNESS_EXPIRING_SOON_DAYS 天未更新 → 即将过期

        Returns:
            同 get_knowledge_freshness 返回格式，source 字段标识数据来源。
        """
        now = datetime.utcnow()
        expire_threshold = now - timedelta(days=FRESHNESS_EXPIRE_DAYS)
        expiring_threshold = now - timedelta(days=FRESHNESS_EXPIRING_SOON_DAYS)

        total_result = await self.db.execute(
            select(func.count(Document.id)).where(Document.deleted_at.is_(None))
        )
        total_docs = total_result.scalar() or 0

        expired_result = await self.db.execute(
            select(func.count(Document.id)).where(
                Document.deleted_at.is_(None),
                Document.updated_at < expire_threshold,
            )
        )
        expired = expired_result.scalar() or 0

        expiring_result = await self.db.execute(
            select(func.count(Document.id)).where(
                Document.deleted_at.is_(None),
                Document.updated_at >= expire_threshold,
                Document.updated_at < expiring_threshold,
            )
        )
        expiring_soon = expiring_result.scalar() or 0

        freshness_rate = round((total_docs - expired) / total_docs, 2) if total_docs > 0 else 1.0

        return {
            "total_documents": total_docs,
            "expired": expired,
            "expiring_soon": expiring_soon,
            "freshness_rate": freshness_rate,
            "source": "postgresql",
        }

    async def get_top_contributors(
        self, days: int = 30, top_k: int = 10,
    ) -> list[dict]:
        """专家贡献排行 — 按文档数+回答数+评论数加权。

        权重：文档 40% + 采纳回答 30% + 评论 30%

        Args:
            days: 统计周期（天）。
            top_k: 返回数量上限。

        Returns:
            [{"user_id": "...", "name": "...", "score": 12.5}, ...]
        """
        since = datetime.utcnow() - timedelta(days=days)

        # 文档作者统计
        doc_result = await self.db.execute(
            select(
                Document.owner_id,
                func.count(Document.id).label("doc_count"),
            )
            .where(
                Document.deleted_at.is_(None),
                Document.created_at >= since,
            )
            .group_by(Document.owner_id)
        )
        doc_counts: dict = {}
        for r in doc_result:
            if r.owner_id:
                doc_counts[str(r.owner_id)] = r.doc_count

        # 采纳回答统计
        answer_result = await self.db.execute(
            select(
                QaAnswer.user_id,
                func.count(QaAnswer.id).label("answer_count"),
                func.sum(cast(QaAnswer.is_accepted, Integer)).label("accepted_count"),
            )
            .where(
                QaAnswer.deleted_at.is_(None),
                QaAnswer.created_at >= since,
            )
            .group_by(QaAnswer.user_id)
        )
        answer_counts: dict = {}
        for r in answer_result:
            if r.user_id:
                answer_counts[str(r.user_id)] = {
                    "answers": r.answer_count,
                    "accepted": r.accepted_count or 0,
                }

        # 评论统计
        comment_result = await self.db.execute(
            select(
                DocumentComment.user_id,
                func.count(DocumentComment.id).label("comment_count"),
            )
            .where(
                DocumentComment.deleted_at.is_(None),
                DocumentComment.created_at >= since,
            )
            .group_by(DocumentComment.user_id)
        )
        comment_counts: dict = {}
        for r in comment_result:
            if r.user_id:
                comment_counts[str(r.user_id)] = r.comment_count

        # 合并计算分数
        all_user_ids = set(doc_counts) | set(answer_counts) | set(comment_counts)
        scores: list[dict] = []
        for uid in all_user_ids:
            doc_score = doc_counts.get(uid, 0) * 0.4
            ans = answer_counts.get(uid, {})
            ans_score = ans.get("answers", 0) * 0.2 + ans.get("accepted", 0) * 0.1
            com_score = comment_counts.get(uid, 0) * 0.3
            total = round(doc_score + ans_score + com_score, 2)
            scores.append({"user_id": uid, "score": total})

        scores.sort(key=lambda x: x["score"], reverse=True)
        return scores[:top_k]

    # ------------------------------------------------------------------
    # 搜索日志记录
    # ------------------------------------------------------------------

    async def log_search(
        self,
        query: str,
        user_id: str | None = None,
        source: str = "knowledge_base",
        result_count: int = 0,
    ) -> None:
        """记录一次搜索行为 — 供仪表盘统计。

        Args:
            query: 搜索关键词。
            user_id: 用户 ID（匿名搜索时为空）。
            source: 搜索源。
            result_count: 返回结果数。
        """
        import uuid as uuid_mod

        log = SearchLog(
            user_id=uuid_mod.UUID(user_id) if user_id else None,
            query=query,
            source=source,
            result_count=result_count,
            clicked=False,
        )
        self.db.add(log)
        await self.db.flush()
