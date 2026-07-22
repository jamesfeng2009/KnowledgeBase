"""
专家发现服务 — 单一职责：基于关键词查找企业内相关专家。

通过四个维度构建用户-主题关联，计算专家权重：
    1. 文档作者（40%）— 写了什么 = 懂什么
    2. 问答回答（30%）— 回答采纳率 = 专业度
    3. 评论活跃（15%）— 参与讨论 = 关注度
    4. 浏览历史（15%）— 常看什么 = 用什么

P0 解耦：贡献排行逻辑内聚到本服务，不再依赖 AnalyticsService。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, and_, cast, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import DocumentComment
from app.models.knowledge import Document
from app.models.qa import QaAnswer
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

logger = get_logger(__name__)

#: 专家分数权重
WEIGHT_DOC: float = 0.4
WEIGHT_ANSWER: float = 0.3
WEIGHT_COMMENT: float = 0.15
WEIGHT_VIEW: float = 0.15

#: 贡献排行权重（与 AnalyticsService 保持一致，保证数据口径统一）
CONTRIB_WEIGHT_DOC: float = 0.4
CONTRIB_WEIGHT_ANSWER: float = 0.2
CONTRIB_WEIGHT_ACCEPTED: float = 0.1
CONTRIB_WEIGHT_COMMENT: float = 0.3


class ExpertService:
    """专家发现服务 — 基于文档/问答/评论计算用户-主题关联。"""

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.db = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    async def find_experts(
        self,
        keyword: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """根据关键词查找相关专家。

        流程：
        1. 搜索包含关键词的文档 → 提取 owner_id
        2. 搜索包含关键词的问答回答 → 提取 user_id
        3. 搜索包含关键词的评论 → 提取 user_id
        4. 按权重计算专家分数，排序返回

        Args:
            keyword: 搜索关键词。
            top_k: 返回专家数量上限。

        Returns:
            专家列表，每项含 user_id/name/department/score。
        """
        if not keyword or not keyword.strip():
            return []

        kw = f"%{keyword.strip()}%"

        # 1. 文档作者统计（权重 40%）
        doc_stmt = (
            select(
                Document.owner_id,
                func.count(Document.id).label("doc_count"),
            )
            .where(
                and_(
                    Document.deleted_at.is_(None),
                    Document.owner_id.isnot(None),
                    Document.title.ilike(kw),
                )
            )
        )
        doc_stmt = apply_tenant_filter(doc_stmt, Document, self._tenant_id)
        doc_stmt = doc_stmt.group_by(Document.owner_id)
        doc_result = await self.db.execute(doc_stmt)
        doc_scores: dict[str, float] = {}
        for r in doc_result:
            if r.owner_id:
                doc_scores[str(r.owner_id)] = r.doc_count * WEIGHT_DOC

        # 2. 问答回答统计（权重 30%，采纳回答额外加权）
        answer_stmt = (
            select(
                QaAnswer.user_id,
                func.count(QaAnswer.id).label("answer_count"),
                func.sum(cast(QaAnswer.is_accepted, Integer)).label("accepted_count"),
            )
            .where(
                and_(
                    QaAnswer.deleted_at.is_(None),
                    QaAnswer.content.ilike(kw),
                )
            )
        )
        answer_stmt = apply_tenant_filter(answer_stmt, QaAnswer, self._tenant_id)
        answer_stmt = answer_stmt.group_by(QaAnswer.user_id)
        answer_result = await self.db.execute(answer_stmt)
        answer_scores: dict[str, float] = {}
        for r in answer_result:
            if r.user_id:
                score = r.answer_count * 0.2 + (r.accepted_count or 0) * 0.1
                answer_scores[str(r.user_id)] = score * WEIGHT_ANSWER / 0.3

        # 3. 评论统计（权重 15%）
        comment_stmt = (
            select(
                DocumentComment.user_id,
                func.count(DocumentComment.id).label("comment_count"),
            )
            .where(
                and_(
                    DocumentComment.deleted_at.is_(None),
                    DocumentComment.content.ilike(kw),
                )
            )
        )
        comment_stmt = apply_tenant_filter(comment_stmt, DocumentComment, self._tenant_id)
        comment_stmt = comment_stmt.group_by(DocumentComment.user_id)
        comment_result = await self.db.execute(comment_stmt)
        comment_scores: dict[str, float] = {}
        for r in comment_result:
            if r.user_id:
                comment_scores[str(r.user_id)] = r.comment_count * WEIGHT_COMMENT

        # 4. 合并计算分数
        all_user_ids = set(doc_scores) | set(answer_scores) | set(comment_scores)
        scores: list[dict[str, Any]] = []
        for uid_str in all_user_ids:
            total = (
                doc_scores.get(uid_str, 0)
                + answer_scores.get(uid_str, 0)
                + comment_scores.get(uid_str, 0)
            )
            scores.append({"user_id": uid_str, "score": round(total, 2)})

        scores.sort(key=lambda x: x["score"], reverse=True)
        top_scores = scores[: top_k * 2]

        # 5. 补充用户信息
        experts: list[dict[str, Any]] = []
        for item in top_scores:
            try:
                user_uid = uuid.UUID(item["user_id"])
            except (ValueError, TypeError):
                continue
            user = await self._get_user_info(user_uid)
            if user:
                experts.append({
                    "user_id": item["user_id"],
                    "name": user.name,
                    "email": user.email,
                    "department": user.dept_id,
                    "score": item["score"],
                })
            if len(experts) >= top_k:
                break

        logger.info(
            "expert.find_experts",
            keyword=keyword,
            found=len(experts),
        )
        return experts

    async def get_user_expertise(self, user_id: str) -> list[dict[str, Any]]:
        """获取用户的专业领域（基于其文档标题关键词）。

        提取用户所有文档标题，按关键词分组统计。

        Args:
            user_id: 用户 ID 字符串。

        Returns:
            专业领域列表，每项含 keyword/doc_count。
        """
        try:
            user_uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return []

        stmt = (
            select(Document.title, Document.category)
            .where(
                and_(
                    Document.owner_id == user_uid,
                    Document.deleted_at.is_(None),
                )
            )
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        stmt = stmt.order_by(desc(Document.created_at)).limit(50)
        result = await self.db.execute(stmt)

        # 按分类统计
        category_counts: dict[str, int] = {}
        for r in result:
            cat = r.category or "未分类"
            category_counts[cat] = category_counts.get(cat, 0) + 1

        expertise = [
            {"keyword": cat, "doc_count": count}
            for cat, count in sorted(
                category_counts.items(), key=lambda x: x[1], reverse=True
            )
        ]
        logger.info(
            "expert.get_expertise",
            user_id=user_id,
            categories=len(expertise),
        )
        return expertise

    async def get_top_contributors(
        self,
        days: int = 30,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """全站贡献排行 — 按文档数+回答数+评论数加权。

        P0 解耦：原由 AnalyticsService 提供，现内聚到 ExpertService，
        使 experts 模块不再依赖 analytics_dashboard 模块。

        权重：文档 40% + 回答 20% + 采纳回答 10% + 评论 30%

        Args:
            days: 统计周期（天）。
            top_k: 返回数量上限。

        Returns:
            [{"user_id": "...", "name": "...", "score": 12.5}, ...]
        """
        since = datetime.utcnow() - timedelta(days=days)

        # 1. 文档作者统计
        doc_stmt = (
            select(
                Document.owner_id,
                func.count(Document.id).label("doc_count"),
            )
            .where(
                Document.deleted_at.is_(None),
                Document.created_at >= since,
            )
        )
        doc_stmt = apply_tenant_filter(doc_stmt, Document, self._tenant_id)
        doc_stmt = doc_stmt.group_by(Document.owner_id)
        doc_result = await self.db.execute(doc_stmt)
        doc_counts: dict[str, int] = {}
        for r in doc_result:
            if r.owner_id:
                doc_counts[str(r.owner_id)] = r.doc_count

        # 2. 问答回答统计（含采纳数）
        answer_stmt = (
            select(
                QaAnswer.user_id,
                func.count(QaAnswer.id).label("answer_count"),
                func.sum(cast(QaAnswer.is_accepted, Integer)).label("accepted_count"),
            )
            .where(
                QaAnswer.deleted_at.is_(None),
                QaAnswer.created_at >= since,
            )
        )
        answer_stmt = apply_tenant_filter(answer_stmt, QaAnswer, self._tenant_id)
        answer_stmt = answer_stmt.group_by(QaAnswer.user_id)
        answer_result = await self.db.execute(answer_stmt)
        answer_counts: dict[str, dict[str, int]] = {}
        for r in answer_result:
            if r.user_id:
                answer_counts[str(r.user_id)] = {
                    "answers": r.answer_count,
                    "accepted": r.accepted_count or 0,
                }

        # 3. 评论统计
        comment_stmt = (
            select(
                DocumentComment.user_id,
                func.count(DocumentComment.id).label("comment_count"),
            )
            .where(
                DocumentComment.deleted_at.is_(None),
                DocumentComment.created_at >= since,
            )
        )
        comment_stmt = apply_tenant_filter(comment_stmt, DocumentComment, self._tenant_id)
        comment_stmt = comment_stmt.group_by(DocumentComment.user_id)
        comment_result = await self.db.execute(comment_stmt)
        comment_counts: dict[str, int] = {}
        for r in comment_result:
            if r.user_id:
                comment_counts[str(r.user_id)] = r.comment_count

        # 4. 合并计算分数
        all_user_ids = set(doc_counts) | set(answer_counts) | set(comment_counts)
        scores: list[dict[str, Any]] = []
        for uid in all_user_ids:
            doc_score = doc_counts.get(uid, 0) * CONTRIB_WEIGHT_DOC
            ans = answer_counts.get(uid, {})
            ans_score = (
                ans.get("answers", 0) * CONTRIB_WEIGHT_ANSWER
                + ans.get("accepted", 0) * CONTRIB_WEIGHT_ACCEPTED
            )
            com_score = comment_counts.get(uid, 0) * CONTRIB_WEIGHT_COMMENT
            total = round(doc_score + ans_score + com_score, 2)
            scores.append({"user_id": uid, "score": total})

        scores.sort(key=lambda x: x["score"], reverse=True)
        top_scores = scores[:top_k]

        # 5. 补充用户姓名
        for item in top_scores:
            try:
                user_uid = uuid.UUID(item["user_id"])
            except (ValueError, TypeError):
                item["name"] = None
                continue
            user = await self._get_user_info(user_uid)
            item["name"] = user.name if user else None

        logger.info(
            "expert.get_top_contributors",
            days=days,
            returned=len(top_scores),
        )
        return top_scores

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _get_user_info(self, user_uid: uuid.UUID) -> User | None:
        """获取用户基本信息。"""
        stmt = select(User).where(User.id == user_uid)
        stmt = apply_tenant_filter(stmt, User, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()
