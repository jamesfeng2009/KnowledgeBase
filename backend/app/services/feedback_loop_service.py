"""
反馈闭环服务 — 单一职责：处理用户反馈并驱动知识库改进。

反馈闭环流程：
1. 接收用户反馈（Feedback 表）；
2. 分析反馈内容，识别关联文档；
3. 将反馈关联到对应文档，驱动质量改进；
4. 基于反馈统计数据生成改进建议。

遵循单一职责：FeedbackLoopService 只负责反馈的闭环处理与分析，
不涉及反馈的创建（委托 FeedbackService）或质量评分（委托 QualityService）。
遵循开闭原则：新增改进建议规则只需扩展 get_improvement_suggestions，
不修改 process_feedback 逻辑。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback
from app.repositories.feedback_repository import FeedbackRepository
from app.utils.logger import get_logger

log = get_logger(__name__)


class FeedbackLoopService:
    """反馈闭环服务 — 处理反馈并驱动知识库改进。

    使用方式::

        service = FeedbackLoopService(db)
        await service.process_feedback(feedback_id)
        await service.link_feedback_to_doc(feedback_id, doc_id)
        stats = await service.get_feedback_stats()
        suggestions = await service.get_improvement_suggestions()
    """

    def __init__(self, db: AsyncSession) -> None:
        """初始化反馈闭环服务。

        Args:
            db: 异步数据库会话。
        """
        self.db: AsyncSession = db
        self.feedback_repo: FeedbackRepository = FeedbackRepository(db)

    # ------------------------------------------------------------------
    # 处理反馈
    # ------------------------------------------------------------------

    async def process_feedback(self, feedback_id: uuid.UUID) -> dict[str, Any]:
        """处理单条用户反馈。

        根据反馈类型和内容，自动流转状态并生成初步处理结论：
        - bug → status=processing, 标记为需调查；
        - suggestion → status=processing, 标记为待评估；
        - praise → status=resolved, 直接关闭；
        - complaint → status=processing, 标记为需处理。

        Args:
            feedback_id: 反馈 ID。

        Returns:
            处理后的反馈详情。

        Raises:
            ValueError: 反馈不存在。
        """
        feedback = await self.feedback_repo.get_by_id(feedback_id)
        if feedback is None:
            raise ValueError(f"反馈不存在: {feedback_id}")

        # 根据反馈类型自动流转状态
        status_map = {
            "bug": "processing",
            "suggestion": "processing",
            "praise": "resolved",
            "complaint": "processing",
        }
        new_status = status_map.get(feedback.type, "processing")

        # 生成自动处理回复
        auto_response = self._generate_auto_response(feedback)
        updated = await self.feedback_repo.update(
            feedback_id,
            status=new_status,
            response=auto_response,
        )

        log.info(
            "feedback_loop.processed",
            feedback_id=str(feedback_id),
            type=feedback.type,
            new_status=new_status,
        )
        return {
            "id": str(updated.id),
            "type": updated.type,
            "status": updated.status,
            "response": updated.response,
            "processed_at": updated.updated_at.isoformat()
            if updated.updated_at
            else None,
        }

    # ------------------------------------------------------------------
    # 关联反馈到文档
    # ------------------------------------------------------------------

    async def link_feedback_to_doc(
        self,
        feedback_id: uuid.UUID,
        doc_id: uuid.UUID,
    ) -> dict[str, Any]:
        """将反馈关联到指定文档。

        在反馈的 response 字段中追加关联文档信息，
        便于后续追溯反馈与文档的关系。

        Args:
            feedback_id: 反馈 ID。
            doc_id: 文档 ID。

        Returns:
            更新后的反馈详情。

        Raises:
            ValueError: 反馈不存在。
        """
        feedback = await self.feedback_repo.get_by_id(feedback_id)
        if feedback is None:
            raise ValueError(f"反馈不存在: {feedback_id}")

        # 在 response 中追加关联文档信息
        existing_response = feedback.response or ""
        link_note = f"[关联文档: {doc_id}]"
        if link_note not in existing_response:
            new_response = (
                f"{existing_response}\n{link_note}".strip()
                if existing_response
                else link_note
            )
            updated = await self.feedback_repo.update(
                feedback_id,
                response=new_response,
            )
        else:
            updated = feedback

        log.info(
            "feedback_loop.linked_to_doc",
            feedback_id=str(feedback_id),
            doc_id=str(doc_id),
        )
        return {
            "id": str(updated.id),
            "type": updated.type,
            "status": updated.status,
            "response": updated.response,
            "linked_doc_id": str(doc_id),
        }

    # ------------------------------------------------------------------
    # 反馈统计
    # ------------------------------------------------------------------

    async def get_feedback_stats(self) -> dict[str, Any]:
        """获取反馈统计数据。

        统计维度：
        - 总数；
        - 按类型分布（bug/suggestion/praise/complaint）；
        - 按状态分布（open/processing/resolved/closed）；
        - 按优先级分布。

        Returns:
            反馈统计字典。
        """
        # 按类型统计
        type_stmt = (
            select(Feedback.type, func.count())
            .group_by(Feedback.type)
        )
        type_result = await self.db.execute(type_stmt)
        type_dist = {row[0]: row[1] for row in type_result.all()}

        # 按状态统计
        status_stmt = (
            select(Feedback.status, func.count())
            .group_by(Feedback.status)
        )
        status_result = await self.db.execute(status_stmt)
        status_dist = {row[0]: row[1] for row in status_result.all()}

        # 按优先级统计
        priority_stmt = (
            select(Feedback.priority, func.count())
            .group_by(Feedback.priority)
        )
        priority_result = await self.db.execute(priority_stmt)
        priority_dist = {row[0]: row[1] for row in priority_result.all()}

        total = sum(type_dist.values())

        stats = {
            "total": total,
            "by_type": type_dist,
            "by_status": status_dist,
            "by_priority": priority_dist,
        }
        log.info("feedback_loop.stats", total=total)
        return stats

    # ------------------------------------------------------------------
    # 改进建议
    # ------------------------------------------------------------------

    async def get_improvement_suggestions(self) -> list[dict[str, Any]]:
        """基于反馈数据生成知识库改进建议。

        建议生成规则：
        1. 高频 bug 反馈（>=3 条同类型）→ 建议排查对应模块；
        2. 高频 suggestion 反馈（>=3 条同类型）→ 建议评估功能需求；
        3. 未处理的 open 状态反馈（>=5 条）→ 建议加快处理进度；
        4. complaint 反馈（>=2 条）→ 建议重点关注用户满意度。

        Returns:
            改进建议列表。
        """
        suggestions: list[dict[str, Any]] = []

        # 获取全部反馈用于分析
        all_feedbacks = await self.feedback_repo.get_all(skip=0, limit=1000)

        # 按类型分组统计
        type_counts: dict[str, list[Feedback]] = {}
        for fb in all_feedbacks:
            type_counts.setdefault(fb.type, []).append(fb)

        # 规则1：高频 bug
        bug_list = type_counts.get("bug", [])
        if len(bug_list) >= 3:
            suggestions.append({
                "type": "bug_cluster",
                "priority": "high",
                "title": "高频缺陷反馈预警",
                "description": f"检测到 {len(bug_list)} 条 bug 反馈，建议优先排查相关模块缺陷。",
                "affected_count": len(bug_list),
                "action": "排查 bug 反馈中涉及的系统模块，优先修复高频问题。",
            })

        # 规则2：高频 suggestion
        suggestion_list = type_counts.get("suggestion", [])
        if len(suggestion_list) >= 3:
            suggestions.append({
                "type": "suggestion_cluster",
                "priority": "medium",
                "title": "功能需求集中反馈",
                "description": f"检测到 {len(suggestion_list)} 条功能建议，建议评估纳入产品路线图。",
                "affected_count": len(suggestion_list),
                "action": "评估用户建议的可行性与优先级，纳入下版本规划。",
            })

        # 规则3：未处理的 open 状态反馈
        open_list = [fb for fb in all_feedbacks if fb.status == "open"]
        if len(open_list) >= 5:
            suggestions.append({
                "type": "backlog_warning",
                "priority": "high",
                "title": "反馈处理积压预警",
                "description": f"当前有 {len(open_list)} 条未处理反馈，建议加快处理进度。",
                "affected_count": len(open_list),
                "action": "增加反馈处理人力，优先处理 high/urgent 优先级反馈。",
            })

        # 规则4：complaint 反馈
        complaint_list = type_counts.get("complaint", [])
        if len(complaint_list) >= 2:
            suggestions.append({
                "type": "complaint_alert",
                "priority": "high",
                "title": "用户投诉预警",
                "description": f"检测到 {len(complaint_list)} 条投诉，建议关注用户满意度。",
                "affected_count": len(complaint_list),
                "action": "分析投诉根因，制定改善措施，必要时主动联系用户。",
            })

        log.info(
            "feedback_loop.suggestions_generated",
            count=len(suggestions),
        )
        return suggestions

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _generate_auto_response(self, feedback: Feedback) -> str:
        """根据反馈类型生成自动处理回复。

        Args:
            feedback: 反馈 ORM 实例。

        Returns:
            自动回复文本。
        """
        responses = {
            "bug": "已收到缺陷反馈，正在调查中，我们会尽快修复。",
            "suggestion": "感谢您的建议，已记录并提交评估。",
            "praise": "感谢您的肯定，我们会继续保持！",
            "complaint": "抱歉给您带来不便，我们正在处理您的投诉。",
        }
        return responses.get(feedback.type, "已收到您的反馈，正在处理中。")
