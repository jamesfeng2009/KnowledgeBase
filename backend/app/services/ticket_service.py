"""
IT 工单服务 — 单一职责：工单的查询 / 创建 / 状态流转业务逻辑。

分层约定与 ``app/services/testing`` 一致：API 层只做序列化，业务规则全部在这里，
异常用 ``ValueError``（不存在）/ ``PermissionError``（越权）/ ``ValueError``（非法流转），
由 ``app.main`` 的全局处理器翻译成 404 / 403。

状态机（刻意收窄）：
    open      → progress / resolved
    progress  → resolved
    resolved  → （终态，不可回退；要复开请新建工单）

为什么 resolved 不可回退：IT 工单是"问题生命周期"的审计载体，回退会让
"平均解决时长""AI 解决率"这类统计失去意义。复开 = 新工单关联旧工单。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ticket import Ticket
from app.models.user import User
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 合法状态流转图
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"progress", "resolved"},
    "progress": {"resolved"},
    "resolved": set(),
}

_VALID_STATUSES = frozenset(_ALLOWED_TRANSITIONS)


class InvalidTransitionError(ValueError):
    """非法状态流转。

    刻意不继承普通 ValueError 的语义：``app.main`` 把 ``ValueError`` 翻译成 404
    （"资源不存在"），而非法流转是 409 Conflict。API 层捕获本类改为 409。
    """


class TicketService:
    """IT 工单服务。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID | None = None) -> None:
        self.db = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _base_conditions(self) -> list:
        conditions = [Ticket.deleted_at.is_(None)]
        if self._tenant_id is not None:
            conditions.append(Ticket.tenant_id == self._tenant_id)
        return conditions

    async def list_tickets(
        self,
        status: str | None = None,
        category: str | None = None,
        page: int = 1,
        size: int = 20,
        user_id: uuid.UUID | None = None,
    ) -> tuple[list[Ticket], int]:
        """分页查询工单 — 按状态/分类过滤，创建时间倒序。

        Args:
            status: 状态过滤（open/progress/resolved）。
            category: 分类过滤。
            page: 页码（从 1 开始）。
            size: 每页数量。
            user_id: 只查某个提交人的工单（员工看自己的工单时用）。

        Returns:
            ``(工单列表, 总数)``。

        Raises:
            ValueError: status 非法。
        """
        conditions = self._base_conditions()
        if status is not None:
            if status not in _VALID_STATUSES:
                raise ValueError(f"非法状态: {status}")
            conditions.append(Ticket.status == status)
        if category:
            conditions.append(Ticket.category == category)
        if user_id is not None:
            conditions.append(Ticket.user_id == user_id)

        total = await self.db.scalar(
            select(func.count()).select_from(Ticket).where(*conditions)
        ) or 0

        result = await self.db.execute(
            select(Ticket)
            .where(*conditions)
            .order_by(Ticket.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(result.scalars().all()), int(total)

    async def get_ticket(self, ticket_id: uuid.UUID) -> Ticket | None:
        """按 ID 取工单（软删除 + 租户过滤）。"""
        result = await self.db.execute(
            select(Ticket).where(Ticket.id == ticket_id, *self._base_conditions())
        )
        return result.scalars().first()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def create_ticket(
        self,
        user: User,
        title: str,
        description: str,
        category: str = "other",
        priority: str = "medium",
    ) -> Ticket:
        """创建工单 — 提交人取当前登录用户。"""
        ticket = Ticket(
            tenant_id=self._tenant_id,
            user_id=user.id,
            user_name=getattr(user, "name", None),
            title=title.strip(),
            description=description.strip(),
            category=category or "other",
            priority=priority or "medium",
            status="open",
            ai_solved=False,
        )
        self.db.add(ticket)
        await self.db.flush()
        log.info(
            "ticket.created",
            ticket_id=str(ticket.id),
            category=ticket.category,
            priority=ticket.priority,
        )
        return ticket

    async def update_ticket(self, ticket_id: uuid.UUID, **fields) -> Ticket:
        """更新工单 — 校验状态流转合法性。

        Args:
            ticket_id: 工单 ID。
            **fields: 待更新字段（None 值被忽略）。

        Returns:
            更新后的工单。

        Raises:
            ValueError: 工单不存在，或状态流转非法。
            PermissionError: 工单属其他租户（不可见）。
        """
        ticket = await self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"工单不存在: {ticket_id}")

        payload = {k: v for k, v in fields.items() if v is not None}

        new_status = payload.get("status")
        if new_status is not None:
            old_status = ticket.status
            if new_status != old_status:
                allowed = _ALLOWED_TRANSITIONS.get(old_status, set())
                if new_status not in allowed:
                    raise InvalidTransitionError(
                        f"非法状态流转: {old_status} → {new_status}"
                        f"（允许: {sorted(allowed) or '无，终态'}）"
                    )

        for key, value in payload.items():
            setattr(ticket, key, value)
        ticket.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        log.info(
            "ticket.updated",
            ticket_id=str(ticket_id),
            fields=sorted(payload.keys()),
        )
        return ticket

    async def resolve_by_ai(
        self,
        ticket_id: uuid.UUID,
        ai_answer: str | None = None,
        ai_solution: str | None = None,
    ) -> Ticket:
        """采纳 AI 解答并关闭工单 — open/progress 均可直接置为 resolved。"""
        return await self.update_ticket(
            ticket_id,
            status="resolved",
            ai_solved=True,
            ai_answer=ai_answer,
            ai_solution=ai_solution,
        )

    async def soft_delete(self, ticket_id: uuid.UUID) -> None:
        """软删除工单。"""
        ticket = await self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"工单不存在: {ticket_id}")
        stmt = (
            update(Ticket)
            .where(Ticket.id == ticket_id, *self._base_conditions())
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await self.db.execute(stmt)
        await self.db.flush()
        log.info("ticket.deleted", ticket_id=str(ticket_id))
