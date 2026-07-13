"""
报表仓储 — 单一职责：报表统计领域的数据访问。

遵循单一职责：ReportRepository 只处理报表统计相关的聚合查询，
不涉及报表格式化或导出逻辑。

遵循开闭原则：每个统计方法封装独立的聚合查询，
新增统计维度只需追加方法，不修改既有实现。

注意：报表查询基于 usage_records（用量记录）和
knowledge_bases / documents（知识库与文档）表，
使用 SQL 聚合函数（COUNT / SUM / AVG）在数据库层完成统计。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import UsageRecord
from app.models.knowledge import Document, KnowledgeBase


class ReportRepository:
    """报表仓储 — 封装报表统计的聚合查询。

    与 BaseRepository 不同，ReportRepository 不绑定单一模型，
    而是跨 usage_records / documents / knowledge_bases 表执行聚合查询。

    所有方法返回原始 dict，由上层 Schema 完成序列化。
    """

    def __init__(self, session: AsyncSession) -> None:
        """初始化报表仓储。

        Args:
            session: 异步数据库会话，由依赖注入 get_db_session 提供。
        """
        self.session: AsyncSession = session

    async def get_usage_stats(
        self, start_date: datetime, end_date: datetime
    ) -> dict[str, Any]:
        """查询指定时间范围内的使用量统计汇总。

        聚合指标：
        - total_queries: 查询次数（usage_records 总行数）
        - unique_users: 独立用户数（DISTINCT user_id）
        - total_tokens: 总 token 消耗（input_tokens + output_tokens 之和）
        - total_cost: 总成本（cost_cents 之和，单位：分）

        Args:
            start_date: 起始时间（包含，>=）。
            end_date: 结束时间（不包含，<）。

        Returns:
            包含聚合指标的 dict。
        """
        stmt = (
            select(
                func.count(UsageRecord.id).label("total_queries"),
                func.count(func.distinct(UsageRecord.user_id)).label("unique_users"),
                func.sum(
                    UsageRecord.input_tokens + UsageRecord.output_tokens
                ).label("total_tokens"),
                func.sum(UsageRecord.cost_cents).label("total_cost_cents"),
            )
            .where(
                UsageRecord.created_at >= start_date,
                UsageRecord.created_at < end_date,
            )
        )
        result = await self.session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return {
                "total_queries": 0,
                "unique_users": 0,
                "total_tokens": 0,
                "total_cost_cents": 0,
            }
        return {
            "total_queries": int(row.total_queries or 0),
            "unique_users": int(row.unique_users or 0),
            "total_tokens": int(row.total_tokens or 0),
            "total_cost_cents": int(row.total_cost_cents or 0),
        }

    async def get_query_logs(
        self,
        start_date: datetime,
        end_date: datetime,
        group_by: str = "day",
    ) -> list[dict[str, Any]]:
        """查询按时间分组的用量记录明细。

        使用 PostgreSQL ``date_trunc`` 函数按 day / week / month 截断时间，
        实现灵活的时间粒度分组。

        Args:
            start_date: 起始时间（包含，>=）。
            end_date: 结束时间（不包含，<）。
            group_by: 分组维度 — day / week / month。

        Returns:
            每个时间分组的统计 dict 列表，包含 period / total_queries /
            unique_users / total_tokens / total_cost_cents。
        """
        # date_trunc 粒度映射
        trunc_map = {
            "day": "day",
            "week": "week",
            "month": "month",
        }
        trunc = trunc_map.get(group_by, "day")

        period = func.date_trunc(trunc, UsageRecord.created_at).label("period")
        stmt = (
            select(
                period,
                func.count(UsageRecord.id).label("total_queries"),
                func.count(func.distinct(UsageRecord.user_id)).label("unique_users"),
                func.sum(
                    UsageRecord.input_tokens + UsageRecord.output_tokens
                ).label("total_tokens"),
                func.sum(UsageRecord.cost_cents).label("total_cost_cents"),
            )
            .where(
                UsageRecord.created_at >= start_date,
                UsageRecord.created_at < end_date,
            )
            .group_by(period)
            .order_by(period)
        )
        result = await self.session.execute(stmt)
        rows = result.all()
        return [
            {
                "period": str(row.period),
                "total_queries": int(row.total_queries or 0),
                "unique_users": int(row.unique_users or 0),
                "total_tokens": int(row.total_tokens or 0),
                "total_cost_cents": int(row.total_cost_cents or 0),
            }
            for row in rows
        ]

    async def get_cost_stats(
        self, start_date: datetime, end_date: datetime
    ) -> dict[str, Any]:
        """查询指定时间范围内的成本统计。

        按 model 和 request_type 分组，返回各维度成本明细。

        Args:
            start_date: 起始时间（包含，>=）。
            end_date: 结束时间（不包含，<）。

        Returns:
            包含 total_cost_cents / total_input_tokens /
            total_output_tokens / by_model / by_request_type 的 dict。
        """
        # 汇总
        summary_stmt = (
            select(
                func.sum(UsageRecord.cost_cents).label("total_cost_cents"),
                func.sum(UsageRecord.input_tokens).label("total_input_tokens"),
                func.sum(UsageRecord.output_tokens).label("total_output_tokens"),
            )
            .where(
                UsageRecord.created_at >= start_date,
                UsageRecord.created_at < end_date,
            )
        )
        summary_result = await self.session.execute(summary_stmt)
        summary_row = summary_result.one_or_none()

        # 按模型分组
        model_stmt = (
            select(
                UsageRecord.model,
                func.sum(UsageRecord.cost_cents).label("cost"),
            )
            .where(
                UsageRecord.created_at >= start_date,
                UsageRecord.created_at < end_date,
            )
            .group_by(UsageRecord.model)
        )
        model_result = await self.session.execute(model_stmt)
        by_model = {
            row.model: float(int(row.cost or 0)) / 100.0
            for row in model_result.all()
        }

        # 按请求类型分组
        type_stmt = (
            select(
                UsageRecord.request_type,
                func.sum(UsageRecord.cost_cents).label("cost"),
            )
            .where(
                UsageRecord.created_at >= start_date,
                UsageRecord.created_at < end_date,
            )
            .group_by(UsageRecord.request_type)
        )
        type_result = await self.session.execute(type_stmt)
        by_request_type = {
            row.request_type: float(int(row.cost or 0)) / 100.0
            for row in type_result.all()
        }

        return {
            "total_cost_cents": int(summary_row.total_cost_cents or 0) if summary_row else 0,
            "total_input_tokens": int(summary_row.total_input_tokens or 0) if summary_row else 0,
            "total_output_tokens": int(summary_row.total_output_tokens or 0) if summary_row else 0,
            "by_model": by_model,
            "by_request_type": by_request_type,
        }

    async def get_knowledge_stats(self) -> dict[str, Any]:
        """查询知识库全局统计。

        聚合指标：
        - total_docs: 文档总数（排除已软删除）
        - total_kbs: 知识库总数（排除已软删除）
        - published_count: 已发布文档数
        - draft_count: 草稿文档数
        - archived_count: 已归档文档数

        Returns:
            包含知识库统计指标的 dict。
        """
        # 文档总数与状态分布
        doc_stmt = (
            select(
                func.count(Document.id).label("total_docs"),
                func.sum(
                    case(
                        (Document.status == "published", 1),
                        else_=0,
                    )
                ).label("published_count"),
                func.sum(
                    case(
                        (Document.status == "draft", 1),
                        else_=0,
                    )
                ).label("draft_count"),
                func.sum(
                    case(
                        (Document.status == "archived", 1),
                        else_=0,
                    )
                ).label("archived_count"),
            )
            .where(Document.deleted_at.is_(None))
        )
        doc_result = await self.session.execute(doc_stmt)
        doc_row = doc_result.one_or_none()

        # 知识库总数
        kb_stmt = (
            select(func.count(KnowledgeBase.id))
            .where(KnowledgeBase.deleted_at.is_(None))
        )
        total_kbs = await self.session.scalar(kb_stmt)

        return {
            "total_docs": int(doc_row.total_docs or 0) if doc_row else 0,
            "total_kbs": int(total_kbs or 0),
            "published_count": int(doc_row.published_count or 0) if doc_row else 0,
            "draft_count": int(doc_row.draft_count or 0) if doc_row else 0,
            "archived_count": int(doc_row.archived_count or 0) if doc_row else 0,
        }
