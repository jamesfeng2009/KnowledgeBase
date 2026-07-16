"""
审核流程服务 — 单一职责：审核提交、查询与审批的业务逻辑编排。

遵循单一职责：AuditService 只负责审核的提交、列表查询与审批/驳回流转，
不涉及数据访问细节（委托 BaseRepository）或通知发送（委托通知服务）。
遵循依赖倒置：通过 AsyncSession 和 User 注入，不直接依赖具体的数据源。

注意：AuditFlow 模型支持软删除（SoftDeleteMixin），
BaseRepository 的查询方法自动过滤 deleted_at IS NULL。
分页查询中使用 ``paginate`` 工具，需手动追加 ``deleted_at`` 过滤条件。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditFlow
from app.models.user import User
from app.repositories.base import BaseRepository
from app.utils.logger import get_logger
from app.utils.pagination import PageResult, PaginationParams, paginate

log = get_logger(__name__)


class AuditService:
    """审核流程服务 — 审核提交、查询与审批的业务编排。

    使用 ``BaseRepository[AuditFlow]`` 进行标准 CRUD，
    对于按状态过滤的分页查询使用 ``paginate`` 工具直接构建 SELECT。

    使用方式::

        service = AuditService(db, current_user)
        audit = await service.submit_for_review("document", doc_id, "normal")
        pending = await service.list_pending(page=1, size=20)
        approved = await service.approve(audit_id, "内容合规，通过")
    """

    def __init__(self, db: AsyncSession, user: User) -> None:
        """初始化审核服务。

        Args:
            db: 异步数据库会话，事务由 ``get_db_session`` 依赖统一管理。
            user: 当前操作用户，用于填充 submitter_id / reviewer_id 和审计日志。
        """
        self._db = db
        self._user = user
        self._repo = BaseRepository(AuditFlow, db)

    async def submit_for_review(
        self,
        resource_type: str,
        resource_id: uuid.UUID,
        priority: str = "normal",
    ) -> AuditFlow:
        """提交资源审核。

        Args:
            resource_type: 资源类型 — document/kb/question。
            resource_id: 资源 ID。
            priority: 优先级 — low/normal/high，默认 normal。

        Returns:
            创建后的 ``AuditFlow`` 对象（status 为 pending）。
        """
        audit = await self._repo.create(
            resource_type=resource_type,
            resource_id=resource_id,
            submitter_id=self._user.id,
            priority=priority,
        )
        log.info(
            "audit.submitted",
            audit_id=str(audit.id),
            resource_type=resource_type,
            resource_id=str(resource_id),
            submitter=str(self._user.id),
        )
        return audit

    async def list_pending(
        self,
        page: int = 1,
        size: int = 20,
    ) -> PageResult:
        """分页查询待审核列表。

        按 priority 降序（high > normal > low）+ 创建时间升序排列，
        高优先级的审核排在最前。

        Args:
            page: 页码，从 1 开始。
            size: 每页数量，上限 100（由 ``PaginationParams`` 强制约束）。

        Returns:
            ``PageResult``，包含当前页待审核列表与分页信息。
        """
        stmt = (
            select(AuditFlow)
            .where(
                AuditFlow.status == "pending",
                AuditFlow.deleted_at.is_(None),
            )
            .order_by(
                AuditFlow.priority.desc(),
                AuditFlow.created_at.asc(),
            )
        )
        params = PaginationParams(page=page, size=size)
        return await paginate(stmt, params, self._db)

    async def approve(
        self,
        audit_id: uuid.UUID,
        comment: str | None = None,
    ) -> AuditFlow:
        """通过审核。

        将审核状态从 pending 变更为 approved，记录审核者和审核意见。
        对于 document 类型的审核，审核通过后自动触发文档发布
       （状态从 pending_review → published）。

        Args:
            audit_id: 审核流程 ID。
            comment: 审核意见（可选）。

        Returns:
            更新后的 ``AuditFlow`` 对象（status 为 approved）。

        Raises:
            ValueError: 审核流程不存在或已处理（非 pending 状态）。
        """
        audit = await self._repo.get_by_id(audit_id)
        if audit is None:
            raise ValueError(f"审核流程不存在: {audit_id}")
        if audit.status != "pending":
            raise ValueError(f"审核流程已处理，当前状态: {audit.status}")

        audit = await self._repo.update(
            audit_id,
            status="approved",
            reviewer_id=self._user.id,
            comment=comment,
        )
        log.info(
            "audit.approved",
            audit_id=str(audit_id),
            reviewer=str(self._user.id),
            resource_type=audit.resource_type,
        )

        # 文档类型审核通过后触发发布
        if audit.resource_type == "document":
            try:
                await self._publish_document_after_approval(
                    str(audit.resource_id)
                )
            except Exception as exc:
                log.warning(
                    "audit.publish_failed",
                    audit_id=str(audit_id),
                    resource_id=str(audit.resource_id),
                    error=str(exc),
                )

        return audit

    async def _publish_document_after_approval(self, doc_id: str) -> None:
        """审核通过后发布文档 — 延迟导入避免循环依赖。

        将文档状态从 pending_review 更新为 published，
        由 document_tasks._publish_document 执行实际发布逻辑。

        Args:
            doc_id: 文档 ID（UUID 字符串）。
        """
        from tasks.document_tasks import _publish_document

        await _publish_document(doc_id)

    async def reject(
        self,
        audit_id: uuid.UUID,
        comment: str | None = None,
    ) -> AuditFlow:
        """驳回审核。

        将审核状态从 pending 变更为 rejected，记录审核者和驳回原因。

        Args:
            audit_id: 审核流程 ID。
            comment: 驳回原因（可选，建议提供）。

        Returns:
            更新后的 ``AuditFlow`` 对象（status 为 rejected）。

        Raises:
            ValueError: 审核流程不存在或已处理（非 pending 状态）。
        """
        audit = await self._repo.get_by_id(audit_id)
        if audit is None:
            raise ValueError(f"审核流程不存在: {audit_id}")
        if audit.status != "pending":
            raise ValueError(f"审核流程已处理，当前状态: {audit.status}")

        audit = await self._repo.update(
            audit_id,
            status="rejected",
            reviewer_id=self._user.id,
            comment=comment,
        )
        log.info(
            "audit.rejected",
            audit_id=str(audit_id),
            reviewer=str(self._user.id),
        )
        return audit
