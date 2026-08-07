"""
知识质量控制服务 — 单一职责：计算文档质量评分与生成质量报告。

质量评分维度：
1. 完整度（completeness）：文档是否包含标题、正文、分类等必要字段；
2. 引用准确率（citation_accuracy）：文档被 AI 回答引用后的用户反馈评分；
3. 用户反馈（feedback_score）：与文档相关的用户反馈正负比例。

遵循单一职责：QualityService 只负责质量评估与报告，
不涉及文档的 CRUD（委托 DocumentRepository）或反馈处理（委托 FeedbackLoopService）。
遵循开闭原则：新增评分维度只需扩展 calculate_quality_score 方法，
不修改 report 生成逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Message
from app.models.feedback import Feedback
from app.models.knowledge import Document
from app.repositories.feedback_repository import FeedbackRepository
from app.repositories.knowledge_repository import DocumentRepository
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

#: 质量评分阈值 — 低于此分数的文档被标记为低质量。
LOW_QUALITY_THRESHOLD: float = 0.6

#: 各维度权重 — 完整度 40%、引用准确率 30%、用户反馈 30%。
WEIGHT_COMPLETENESS: float = 0.4
WEIGHT_CITATION: float = 0.3
WEIGHT_FEEDBACK: float = 0.3


class QualityService:
    """知识质量控制服务 — 文档质量评分与报告。

    使用方式::

        service = QualityService(db)
        score = await service.calculate_quality_score(doc_id)
        report = await service.get_quality_report(kb_id)
        low_docs = await service.get_low_quality_docs(threshold=0.6)
    """

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        """初始化质量控制服务。

        Args:
            db: 异步数据库会话。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self._tenant_id = tenant_id
        self.doc_repo: DocumentRepository = DocumentRepository(
            db, tenant_id=tenant_id
        )
        self.feedback_repo: FeedbackRepository = FeedbackRepository(
            db, tenant_id=tenant_id
        )

    # ------------------------------------------------------------------
    # 单文档质量评分
    # ------------------------------------------------------------------

    async def calculate_quality_score(self, doc_id: uuid.UUID) -> dict[str, Any]:
        """计算单个文档的质量评分。

        评分维度（加权综合）：
        - 完整度（40%）：标题、正文、分类等字段完整度；
        - 引用准确率（30%）：基于该文档关联的反馈中正面评价比例；
        - 用户反馈（30%）：该文档相关反馈的满意度。

        Args:
            doc_id: 文档 ID。

        Returns:
            质量评分详情字典，包含总分和各维度分数。

        Raises:
            ValueError: 文档不存在。
        """
        doc = await self.doc_repo.get_by_id(doc_id)
        if doc is None:
            raise ValueError(f"文档不存在: {doc_id}")

        # 1. 完整度评分
        completeness = self._score_completeness(doc)

        # 2. 引用准确率评分（基于反馈）
        citation_score = await self._score_citation(doc_id)

        # 3. 用户反馈评分
        feedback_score = await self._score_feedback(doc_id)

        # 加权综合
        total = (
            completeness * WEIGHT_COMPLETENESS
            + citation_score * WEIGHT_CITATION
            + feedback_score * WEIGHT_FEEDBACK
        )

        result = {
            "doc_id": str(doc_id),
            "title": doc.title,
            "total_score": round(total, 3),
            "completeness": round(completeness, 3),
            "citation_accuracy": round(citation_score, 3),
            "feedback_score": round(feedback_score, 3),
            "is_low_quality": total < LOW_QUALITY_THRESHOLD,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            "quality.scored",
            doc_id=str(doc_id),
            total_score=total,
        )
        return result

    # ------------------------------------------------------------------
    # 质量报告
    # ------------------------------------------------------------------

    async def get_quality_report(
        self,
        kb_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """获取知识库质量报告（可按知识库过滤）。

        汇总统计指标：
        - 文档总数；
        - 平均质量评分；
        - 低质量文档数量；
        - 各质量区间分布。

        Args:
            kb_id: 知识库 ID（可选，为 None 时统计全部）。

        Returns:
            质量报告字典。
        """
        # 查询文档列表
        if kb_id is not None:
            docs = await self.doc_repo.get_by_kb(kb_id)
        else:
            # 全部文档（排除已软删除）
            stmt = select(Document).where(Document.deleted_at.is_(None))
            stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
            result = await self.db.execute(stmt)
            docs = list(result.scalars().all())

        total_docs = len(docs)
        if total_docs == 0:
            return {
                "kb_id": str(kb_id) if kb_id else "all",
                "total_docs": 0,
                "average_score": 0.0,
                "low_quality_count": 0,
                "score_distribution": {},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        # 逐个评分（文档量大时建议异步批量处理）
        scores: list[float] = []
        low_count = 0
        for doc in docs:
            try:
                score_result = await self.calculate_quality_score(doc.id)
                score = score_result["total_score"]
                scores.append(score)
                if score < LOW_QUALITY_THRESHOLD:
                    low_count += 1
            except Exception as exc:
                log.warning("quality.score_failed", doc_id=str(doc.id), error=str(exc))

        avg_score = sum(scores) / len(scores) if scores else 0.0

        # 质量区间分布
        distribution = {
            "excellent (>=0.8)": sum(1 for s in scores if s >= 0.8),
            "good (0.6-0.8)": sum(1 for s in scores if 0.6 <= s < 0.8),
            "low (<0.6)": sum(1 for s in scores if s < 0.6),
        }

        report = {
            "kb_id": str(kb_id) if kb_id else "all",
            "total_docs": total_docs,
            "scored_docs": len(scores),
            "average_score": round(avg_score, 3),
            "low_quality_count": low_count,
            "score_distribution": distribution,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            "quality.report_generated",
            kb_id=str(kb_id) if kb_id else "all",
            total_docs=total_docs,
            avg_score=avg_score,
        )
        return report

    # ------------------------------------------------------------------
    # 低质量文档
    # ------------------------------------------------------------------

    async def get_low_quality_docs(
        self,
        threshold: float = LOW_QUALITY_THRESHOLD,
    ) -> list[dict[str, Any]]:
        """获取质量评分低于阈值的文档列表。

        Args:
            threshold: 质量评分阈值，默认 0.6。

        Returns:
            低质量文档详情列表（含评分信息）。
        """
        stmt = select(Document).where(Document.deleted_at.is_(None))
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        result = await self.db.execute(stmt)
        docs = list(result.scalars().all())

        low_quality: list[dict[str, Any]] = []
        for doc in docs:
            try:
                score_result = await self.calculate_quality_score(doc.id)
                if score_result["total_score"] < threshold:
                    low_quality.append(score_result)
            except Exception as exc:
                log.warning("quality.check_failed", doc_id=str(doc.id), error=str(exc))

        log.info(
            "quality.low_quality_found",
            count=len(low_quality),
            threshold=threshold,
        )
        return low_quality

    # ------------------------------------------------------------------
    # 内部评分方法
    # ------------------------------------------------------------------

    def _score_completeness(self, doc: Document) -> float:
        """计算文档完整度评分（0.0-1.0）。

        检查字段：标题、纯文本内容、HTML 内容、分类、文件路径。
        每个字段存在得 0.2 分，满分为 1.0。

        Args:
            doc: 文档 ORM 实例。

        Returns:
            完整度评分（0.0-1.0）。
        """
        fields = [
            bool(doc.title),
            bool(doc.content_text),
            bool(doc.content_html),
            bool(doc.classification),
            bool(doc.file_path),
        ]
        return sum(1 for f in fields if f) / len(fields)

    async def _load_doc_feedbacks(self, doc_id: uuid.UUID) -> list[Feedback]:
        """查询与指定文档关联的用户反馈。

        优先直查：``Feedback.doc_id`` 冗余列（P0 doc_id 维度，写入时落库）。
        兼容兜底：doc_id 为 NULL 的旧数据仍走
        ``Feedback.related_message_id`` → ``Message.sources``
        （JSONB 引用卡片列表，每项含 doc_id）链路。
        两条链路按 doc_id IS NULL 互斥切分，天然无重复计数。
        """
        # 1. 直查：新数据按 doc_id 冗余列命中
        stmt = select(Feedback).where(Feedback.doc_id == doc_id)
        stmt = apply_tenant_filter(stmt, Feedback, self._tenant_id)
        result = await self.db.execute(stmt)
        direct = list(result.scalars().all())

        # 2. 兼容兜底：仅扫描 doc_id 为 NULL 且关联了消息的旧数据
        legacy_stmt = select(Feedback).where(
            Feedback.doc_id.is_(None),
            Feedback.related_message_id.isnot(None),
        )
        legacy_stmt = apply_tenant_filter(legacy_stmt, Feedback, self._tenant_id)
        legacy_result = await self.db.execute(legacy_stmt)
        legacy = list(legacy_result.scalars().all())
        if not legacy:
            return direct

        msg_stmt = select(Message.id, Message.sources).where(
            Message.id.in_({f.related_message_id for f in legacy})
        )
        msg_rows = (await self.db.execute(msg_stmt)).all()
        # 消息 ID → 该消息引用来源中的 doc_id 集合
        msg_doc_map: dict[uuid.UUID, set[str]] = {}
        for msg_id, sources in msg_rows:
            doc_ids: set[str] = set()
            for source in sources or []:
                if isinstance(source, dict) and source.get("doc_id"):
                    doc_ids.add(str(source["doc_id"]))
            msg_doc_map[msg_id] = doc_ids

        target = str(doc_id)
        legacy_matched = [
            f
            for f in legacy
            if target in msg_doc_map.get(f.related_message_id, set())
        ]
        return direct + legacy_matched

    async def _score_citation(self, doc_id: uuid.UUID) -> float:
        """计算文档引用准确率评分（0.0-1.0）。

        基于该文档关联反馈中正面反馈（praise）的比例。
        无反馈时返回默认分 0.5（中性）。

        Args:
            doc_id: 文档 ID。

        Returns:
            引用准确率评分（0.0-1.0）。
        """
        feedbacks = await self._load_doc_feedbacks(doc_id)

        if not feedbacks:
            return 0.5  # 无反馈时中性评分

        positive = sum(1 for f in feedbacks if f.type == "praise")
        negative = sum(
            1 for f in feedbacks if f.type in ("bug", "complaint")
        )
        total = positive + negative
        if total == 0:
            return 0.5
        return positive / total

    async def _score_feedback(self, doc_id: uuid.UUID) -> float:
        """计算用户反馈评分（0.0-1.0）。

        基于该文档关联反馈中 resolved/closed 状态的占比。
        无反馈时返回默认分 0.5（中性）。

        Args:
            doc_id: 文档 ID。

        Returns:
            用户反馈评分（0.0-1.0）。
        """
        feedbacks = await self._load_doc_feedbacks(doc_id)

        if not feedbacks:
            return 0.5

        resolved = sum(1 for f in feedbacks if f.status == "resolved")
        closed = sum(1 for f in feedbacks if f.status == "closed")
        total = len(feedbacks)

        # resolved + closed 视为正面处理
        positive = resolved + closed
        return positive / total if total > 0 else 0.5
