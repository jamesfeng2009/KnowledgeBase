"""
用户反馈服务 — 单一职责：反馈的业务逻辑编排。

遵循单一职责：FeedbackService 只负责反馈的创建、查询、回复与状态流转，
不涉及数据访问细节（委托 FeedbackRepository）或 API 序列化（委托 Schema 层）。
遵循依赖倒置：通过 AsyncSession 和 User 注入，不直接依赖具体的数据源。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback
from app.models.conversation import Message
from app.models.user import User
from app.repositories.feedback_repository import FeedbackRepository
from app.utils.logger import get_logger
from app.utils.pagination import PageResult, PaginationParams, paginate
from app.utils.tenant import apply_tenant_filter

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

    def __init__(
        self, db: AsyncSession, user: User, tenant_id: UUID | None = None
    ) -> None:
        """初始化反馈服务。

        Args:
            db: 异步数据库会话，事务由 ``get_db_session`` 依赖统一管理。
            user: 当前操作用户，用于填充 user_id 和审计日志。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self._db = db
        self._user = user
        self._tenant_id = tenant_id
        self._repo = FeedbackRepository(db, tenant_id=tenant_id)

    def _require_admin(self) -> None:
        """要求当前用户具备管理员角色（admin/kb_admin）。

        反馈的"官方回复"与状态流转属于处置操作 —— 无角色校验时
        任意用户可伪造官方回复、关闭他人反馈，污染反馈闭环。
        """
        if self._user.role not in ("admin", "kb_admin"):
            raise PermissionError("仅管理员可处理用户反馈")

    async def create_feedback(
        self,
        type: str,
        content: str,
        related_message_id: uuid.UUID | None = None,
        doc_id: uuid.UUID | None = None,
    ) -> Feedback:
        """创建用户反馈。

        Args:
            type: 反馈类型 — bug/suggestion/praise/complaint。
            content: 反馈内容。
            related_message_id: 可选，关联的对话消息 ID。
            doc_id: 可选，关联文档 ID（质量评分 doc_id 维度）。
                    缺省时若传了 related_message_id，则从该消息的
                    引用来源（Message.sources）解析第一个 doc_id 兜底。

        Returns:
            创建后的 ``Feedback`` 对象（status 默认 open，priority 默认 normal）。
        """
        if doc_id is None and related_message_id is not None:
            doc_id = await self._resolve_doc_id_from_message(related_message_id)
        feedback = await self._repo.create(
            user_id=self._user.id,
            type=type,
            content=content,
            related_message_id=related_message_id,
            doc_id=doc_id,
        )
        log.info(
            "feedback.created",
            feedback_id=str(feedback.id),
            user_id=str(self._user.id),
            type=type,
            doc_id=str(doc_id) if doc_id else None,
        )

        # P0: 好评反馈 → 知识库 FAQ 回流触发
        # 仅 praise 且关联了 message 时触发；Celery 不可用时优雅降级（仅日志，不阻断反馈创建）。
        if feedback.type == "praise" and feedback.related_message_id is not None:
            try:
                from tasks.compounding_tasks import (
                    trigger_chat_feedback_compounding,
                )

                trigger_chat_feedback_compounding.delay(
                    str(feedback.id),
                    str(self._tenant_id) if self._tenant_id else None,
                )
                log.info(
                    "feedback.compounding_triggered",
                    feedback_id=str(feedback.id),
                )
            except Exception as exc:
                log.warning(
                    "feedback.compounding_trigger_failed",
                    feedback_id=str(feedback.id),
                    error=str(exc)[:200],
                )

        return feedback

    async def _resolve_doc_id_from_message(
        self, message_id: uuid.UUID
    ) -> uuid.UUID | None:
        """从消息引用来源（JSONB 引用卡片列表）解析第一个 doc_id。

        解析失败（消息不存在 / sources 为空 / 无合法 UUID）时返回 None，
        不阻断反馈创建。
        """
        stmt = select(Message.sources).where(Message.id == message_id)
        sources = (await self._db.execute(stmt)).scalar_one_or_none()
        for source in sources or []:
            if isinstance(source, dict) and source.get("doc_id"):
                try:
                    return uuid.UUID(str(source["doc_id"]))
                except ValueError:
                    continue
        return None

    async def list_feedback(
        self,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> PageResult:
        """分页查询反馈列表，可按状态过滤。

        权限规则：管理员（admin/kb_admin，口径同 ``_require_admin``）可查看
        全租户反馈；普通用户仅能查看自己提交的反馈，避免越权读取他人反馈。

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
        # 权限过滤：非管理员仅能看到自己的反馈
        if self._user.role not in ("admin", "kb_admin"):
            stmt = stmt.where(Feedback.user_id == self._user.id)
        stmt = apply_tenant_filter(stmt, Feedback, self._tenant_id)
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
            PermissionError: 非管理员用户。
        """
        self._require_admin()
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
            PermissionError: 非管理员用户。
        """
        self._require_admin()
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
