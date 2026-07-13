"""
用户反馈服务 — 单一职责：反馈的业务逻辑编排。

遵循单一职责：FeedbackService 只负责反馈的创建、查询、回复与状态流转，
不涉及数据访问细节（委托 FeedbackRepository）或 API 序列化（委托 Schema 层）。
遵循依赖倒置：通过 AsyncSession 和 User 注入，不直接依赖具体的数据源。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback
from app.models.user import User
from app.repositories.feedback_repository import FeedbackRepository
from app.utils.logger import get_logger
from app.utils.pagination import PageResult, PaginationParams, paginate

log = get_logger(__name__)


class FeedbackService:
    """用户反馈管理服务 — 反馈闭环的业务编排。

    通过 ``FeedbackRepository`` 进行数据访问，通过 ``paginate`` 工具
    执行分页查询，业务层不直接拼接 SQL（分页查询除外）。

    使用方式::

        service = FeedbackService(db, current_user)
        feedback = await service.create_feedback("bug", "页面加载缓慢")
        page = await service.list_feedback(status="open", page=1, size=20)
    """

    def __init__(self, db: AsyncSession, user: User) -> None:
        """初始化反馈服务。

        Args:
            db: 异步数据库会话，事务由 ``get_db_session`` 依赖统一管理。
            user: 当前操作用户，用于填充 user_id 和审计日志。
        """
        self._db = db
        self._user = user
        self._repo = FeedbackRepository(db)

    async def create_feedback(
        self,
        type: str,
        content: str,
        related_message_id: uuid.UUID | None = None,
    ) -> Feedback:
        """创建用户反馈。

        Args:
            type: 反馈类型 — bug/suggestion/praise/complaint。
            content: 反馈内容。
            related_message_id: 可选，关联的对话消息 ID。

        Returns:
            创建后的 ``Feedback`` 对象（status 默认 open，priority 默认 normal）。
        """
        feedback = await self._repo.create(
            user_id=self._user.id,
            type=type,
            content=content,
            related_message_id=related_message_id,
        )
        log.info(
            "feedback.created",
            feedback_id=str(feedback.id),
            user_id=str(self._user.id),
            type=type,
        )
        return feedback

    async def list_feedback(
        self,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> PageResult:
        """分页查询反馈列表，可按状态过滤。

        Args:
            status: 可选，反馈状态 — open/processing/resolved/closed。
                    为 ``None`` 时查询全部状态。
            page: 页码，从 1 开始。
            size: 每页数量，上限 100（由 ``PaginationParams`` 强制约束）。

        Returns:
            ``PageResult``，包含当前页反馈列表与分页信息。
        """
        stmt = select(Feedback)
        if status:
            stmt = stmt.where(Feedback.status == status)
        stmt = stmt.order_by(Feedback.created_at.desc())

        params = PaginationParams(page=page, size=size)
        return await paginate(stmt, params, self._db)

    async def respond_to_feedback(
        self,
        feedback_id: uuid.UUID,
        response: str,
    ) -> Feedback:
        """回复用户反馈，同时将状态置为 processing。

        Args:
            feedback_id: 反馈 ID。
            response: 处理回复内容。

        Returns:
            更新后的 ``Feedback`` 对象。

        Raises:
            ValueError: 反馈不存在。
        """
        feedback = await self._repo.get_by_id(feedback_id)
        if feedback is None:
            raise ValueError(f"反馈不存在: {feedback_id}")

        feedback = await self._repo.update(
            feedback_id,
            response=response,
            status="processing",
        )
        log.info(
            "feedback.responded",
            feedback_id=str(feedback_id),
            responder=str(self._user.id),
        )
        return feedback

    async def update_feedback_status(
        self,
        feedback_id: uuid.UUID,
        status: str,
    ) -> Feedback:
        """更新反馈状态。

        Args:
            feedback_id: 反馈 ID。
            status: 新状态 — open/processing/resolved/closed。

        Returns:
            更新后的 ``Feedback`` 对象。

        Raises:
            ValueError: 反馈不存在。
        """
        feedback = await self._repo.get_by_id(feedback_id)
        if feedback is None:
            raise ValueError(f"反馈不存在: {feedback_id}")

        feedback = await self._repo.update(feedback_id, status=status)
        log.info(
            "feedback.status_updated",
            feedback_id=str(feedback_id),
            status=status,
            operator=str(self._user.id),
        )
        return feedback
