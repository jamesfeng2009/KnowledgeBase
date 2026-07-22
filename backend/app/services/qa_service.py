"""
问答社区服务 — 单一职责：编排企业问答（Q&A）的业务流程。

遵循单一职责：QaService 只负责问答领域的业务编排（提问 / 回答 / 采纳 / 浏览计数），
不直接编写 SQL（委托 Repository），不感知 HTTP 层。

遵循开闭原则：通过依赖注入组合 QaQuestionRepository 与 QaAnswerRepository，
新增问答能力只需追加方法，不修改既有方法实现。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qa import QaAnswer, QaQuestion
from app.models.user import User
from app.repositories.qa_repository import QaAnswerRepository, QaQuestionRepository
from app.utils.pagination import PageResult, PaginationParams, paginate
from app.utils.tenant import apply_tenant_filter


class QaService:
    """问答社区服务 — 封装问答帖与回答的业务编排。

    异常策略：资源不存在抛 ValueError，权限不足抛 PermissionError，
    由上层 API 层统一翻译为 HTTP 状态码。
    """

    def __init__(
        self, db: AsyncSession, user: User, tenant_id: UUID | None = None
    ) -> None:
        """初始化问答服务，注入依赖。

        Args:
            db: 异步数据库会话。
            user: 当前已认证用户。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: User = user
        self._tenant_id = tenant_id
        self.question_repo: QaQuestionRepository = QaQuestionRepository(
            db, tenant_id=tenant_id
        )
        self.answer_repo: QaAnswerRepository = QaAnswerRepository(
            db, tenant_id=tenant_id
        )

    # ------------------------------------------------------------------
    # 提问
    # ------------------------------------------------------------------

    async def create_question(
        self,
        kb_id: UUID | None,
        title: str,
        content: str,
        tags: str | None = None,
    ) -> QaQuestion:
        """创建一条问答帖。

        Args:
            kb_id: 关联知识库 ID（可选，用于将问题归类到某知识库）。
            title: 问题标题。
            content: 问题详情。
            tags: 标签（逗号分隔字符串，可选）。

        Returns:
            创建后的 QaQuestion 实例。
        """
        return await self.question_repo.create(
            user_id=self.user.id,
            kb_id=kb_id,
            title=title,
            content=content,
            tags=tags,
            status="open",
        )

    async def list_questions(
        self, status: str | None, page: int, size: int
    ) -> PageResult:
        """分页查询问题列表。

        Args:
            status: 状态过滤 — open / answered / closed；传 None 表示不过滤。
            page: 页码（从 1 开始）。
            size: 每页条数（上限 100）。

        Returns:
            PageResult[QaQuestion]。
        """
        params = PaginationParams(page=page, size=size)
        stmt = select(QaQuestion).where(QaQuestion.deleted_at.is_(None))

        if status:
            stmt = stmt.where(QaQuestion.status == status)

        stmt = apply_tenant_filter(stmt, QaQuestion, self._tenant_id)
        stmt = stmt.order_by(QaQuestion.created_at.desc())
        return await paginate(stmt, params, self.db)

    async def get_question(self, question_id: UUID) -> QaQuestion:
        """获取问题详情并原子递增浏览数。

        浏览数递增使用 ``UPDATE ... SET view_count = view_count + 1`` 原子操作，
        避免并发场景下的竞态条件；递增后 refresh 返回最新计数。

        Args:
            question_id: 问题 ID。

        Returns:
            QaQuestion 实例（含最新浏览数）。

        Raises:
            ValueError: 问题不存在。
        """
        question = await self.question_repo.get_by_id(question_id)
        if question is None:
            raise ValueError(f"问题 {question_id} 不存在")

        await self.question_repo.increment_view(question_id)
        await self.db.refresh(question)
        return question

    # ------------------------------------------------------------------
    # 回答
    # ------------------------------------------------------------------

    async def create_answer(
        self,
        question_id: UUID,
        content: str,
        is_ai_generated: bool = False,
    ) -> QaAnswer:
        """为指定问题创建回答。

        创建回答后同步更新问题的 answer_count 与状态（open → answered）。

        Args:
            question_id: 问题 ID。
            content: 回答内容。
            is_ai_generated: 是否由 AI 生成（默认 False，人工回答）。

        Returns:
            创建后的 QaAnswer 实例。

        Raises:
            ValueError: 问题不存在。
        """
        question = await self.question_repo.get_by_id(question_id)
        if question is None:
            raise ValueError(f"问题 {question_id} 不存在")

        answer = await self.answer_repo.create(
            question_id=question_id,
            user_id=self.user.id,
            content=content,
            is_ai_generated=is_ai_generated,
        )

        # 同步更新问题计数与状态
        await self.question_repo.update(
            question_id,
            answer_count=question.answer_count + 1,
            status="answered",
        )
        return answer

    async def accept_answer(self, answer_id: UUID) -> QaAnswer:
        """采纳指定回答（问题作者或 admin 可操作）。

        采纳后自动取消同一问题下其他回答的采纳状态（由仓储保证原子性），
        并将问题状态置为 answered。

        Args:
            answer_id: 回答 ID。

        Returns:
            采纳后的 QaAnswer 实例。

        Raises:
            ValueError: 回答不存在。
            PermissionError: 当前用户无权采纳（仅问题作者或 admin）。
        """
        answer = await self.answer_repo.get_by_id(answer_id)
        if answer is None:
            raise ValueError(f"回答 {answer_id} 不存在")

        # 仅问题作者或 admin 可采纳
        question = await self.question_repo.get_by_id(answer.question_id)
        if question is None:
            raise ValueError(f"问题 {answer.question_id} 不存在")
        if question.user_id != self.user.id and self.user.role != "admin":
            raise PermissionError("仅问题作者或管理员可采纳回答")

        accepted = await self.answer_repo.accept_answer(answer_id)
        if accepted is None:
            raise ValueError(f"回答 {answer_id} 不存在")

        # 更新问题状态
        await self.question_repo.update(answer.question_id, status="answered")
        return accepted

    async def list_answers(self, question_id: UUID) -> list[QaAnswer]:
        """查询指定问题下的全部回答。

        排序规则：已采纳优先，其次按投票数降序，最后按时间正序。
        排序逻辑由 QaAnswerRepository.get_by_question 实现。

        Args:
            question_id: 问题 ID。

        Returns:
            QaAnswer 列表。

        Raises:
            ValueError: 问题不存在。
        """
        question = await self.question_repo.get_by_id(question_id)
        if question is None:
            raise ValueError(f"问题 {question_id} 不存在")
        return await self.answer_repo.get_by_question(question_id)
