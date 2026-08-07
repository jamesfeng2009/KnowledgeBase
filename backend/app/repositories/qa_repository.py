"""
问答社区仓储 — 单一职责：企业问答（Q&A）领域的数据访问。

遵循单一职责：
- QaQuestionRepository 只处理问题表的查询；
- QaAnswerRepository 只处理回答表的查询。

遵循开闭原则：
- 两者均继承 BaseRepository 获得标准 CRUD；
- 扩展添加各自领域的专属查询和操作方法。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qa import QaAnswer, QaQuestion
from app.repositories.base import BaseRepository


class QaQuestionRepository(BaseRepository[QaQuestion]):
    """问答帖仓储 — 封装问题表的领域查询。"""

    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        super().__init__(QaQuestion, session, tenant_id=tenant_id)

    async def get_open_questions(self) -> list[QaQuestion]:
        """查询所有未关闭的问题（status = open 或 answered，排除已软删除）。

        排除 status = closed 的问题，返回待处理的问题列表。
        """
        stmt = (
            select(QaQuestion)
            .where(
                QaQuestion.status != "closed",
                QaQuestion.deleted_at.is_(None),
            )
        )
        stmt = self._apply_all_filters(stmt)
        stmt = stmt.order_by(QaQuestion.created_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_kb(self, kb_id: UUID) -> list[QaQuestion]:
        """查询关联到指定知识库的所有问题（排除已软删除）。"""
        stmt = (
            select(QaQuestion)
            .where(
                QaQuestion.kb_id == kb_id,
                QaQuestion.deleted_at.is_(None),
            )
        )
        stmt = self._apply_all_filters(stmt)
        stmt = stmt.order_by(QaQuestion.created_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def increment_view(self, question_id: UUID) -> bool:
        """原子递增问题浏览数（排除已软删除）。

        使用 UPDATE ... SET view_count = view_count + 1 原子操作，
        避免并发场景下的竞态条件（先读后写导致丢失更新）。

        Returns:
            True 表示成功递增（记录存在且未删除），False 表示未找到记录。
        """
        stmt = (
            update(QaQuestion)
            .where(
                QaQuestion.id == question_id,
                QaQuestion.deleted_at.is_(None),
            )
        )
        if self._tenant_id is not None:
            stmt = stmt.where(QaQuestion.tenant_id == self._tenant_id)
        stmt = stmt.values(view_count=QaQuestion.view_count + 1)
        result = await self.session.execute(stmt)
        return (result.rowcount or 0) > 0

    async def increment_answer_count(self, question_id: UUID) -> bool:
        """原子递增问题回答数（排除已软删除）。

        使用 UPDATE ... SET answer_count = answer_count + 1 原子操作，
        避免并发创建回答时"先读后写"导致的丢失更新（与 increment_view 同构）。

        Returns:
            True 表示成功递增（记录存在且未删除），False 表示未找到记录。
        """
        stmt = (
            update(QaQuestion)
            .where(
                QaQuestion.id == question_id,
                QaQuestion.deleted_at.is_(None),
            )
        )
        if self._tenant_id is not None:
            stmt = stmt.where(QaQuestion.tenant_id == self._tenant_id)
        stmt = stmt.values(answer_count=QaQuestion.answer_count + 1)
        result = await self.session.execute(stmt)
        return (result.rowcount or 0) > 0


class QaAnswerRepository(BaseRepository[QaAnswer]):
    """回答仓储 — 封装回答表的领域查询。"""

    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        super().__init__(QaAnswer, session, tenant_id=tenant_id)

    async def get_by_question(self, question_id: UUID) -> list[QaAnswer]:
        """查询指定问题下的所有回答（排除已软删除）。

        已采纳的回答排在最前，其余按投票数降序排列。
        """
        stmt = (
            select(QaAnswer)
            .where(
                QaAnswer.question_id == question_id,
                QaAnswer.deleted_at.is_(None),
            )
        )
        stmt = self._apply_all_filters(stmt)
        stmt = stmt.order_by(
            QaAnswer.is_accepted.desc(),
            QaAnswer.vote_count.desc(),
            QaAnswer.created_at.asc(),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def accept_answer(self, answer_id: UUID) -> QaAnswer | None:
        """采纳指定回答。

        操作流程：
        1. 查询目标回答（排除已软删除），不存在则返回 None；
        2. 批量取消同一问题下其他回答的采纳状态（原子 UPDATE）；
        3. 将目标回答的 is_accepted 设为 True；
        4. flush + refresh 返回最新状态。

        Returns:
            采纳后的回答对象，若回答不存在则返回 None。
        """
        answer = await self.get_by_id(answer_id)
        if answer is None:
            return None

        # 先取消该问题下其他回答的采纳状态
        unaccept_stmt = (
            update(QaAnswer)
            .where(
                QaAnswer.question_id == answer.question_id,
                QaAnswer.id != answer_id,
                QaAnswer.deleted_at.is_(None),
            )
        )
        if self._tenant_id is not None:
            unaccept_stmt = unaccept_stmt.where(QaAnswer.tenant_id == self._tenant_id)
        unaccept_stmt = unaccept_stmt.values(is_accepted=False)
        await self.session.execute(unaccept_stmt)

        # 采纳当前回答
        answer.is_accepted = True
        await self.session.flush()
        await self.session.refresh(answer)
        return answer
