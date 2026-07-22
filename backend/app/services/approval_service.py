"""
工具审批服务 — 单一职责：审批记录的 CRUD + 恢复机制。

P1 核心：当 DangerousToolGuard 拦截危险工具时，将审批请求持久化到 DB，
支持服务重启后恢复未决审批，以及前端通过 REST 端点审批/拒绝。

关键设计：
    - JSONB 快照：审批创建时存储 AgentState 快照，恢复时直接反序列化；
    - 会话级控制：审批状态缓存在内存中（session_id → set[tool_name]），
      对话结束后自动清理；
    - 过期机制：默认 1 小时过期，启动时扫描并标记 expired。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import ToolApproval
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

# 审批默认过期时间（秒）
_APPROVAL_TTL_SECONDS: int = 3600  # 1 小时


class ApprovalService:
    """工具审批服务 — 审批记录 CRUD + 恢复 + 会话级缓存。"""

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.db: AsyncSession = db
        self._tenant_id = tenant_id
        # 会话级确认缓存：session_id → set[tool_name]
        # 用于引擎快速判断工具是否已确认，避免每次查 DB
        self._session_confirmed: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # 创建审批请求
    # ------------------------------------------------------------------

    async def create_approval(
        self,
        user_id: uuid.UUID,
        session_id: str,
        tool_name: str,
        tool_use_id: str,
        tool_arguments: dict[str, Any],
        reason: str,
        irreversible: bool,
        agent_state_snapshot: dict[str, Any],
        tenant_id: uuid.UUID | None = None,
    ) -> ToolApproval:
        """创建审批请求 — 当 DangerousToolGuard 拦截危险工具时调用。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。
            tool_name: 被拦截的工具名称。
            tool_use_id: LLM 返回的 tool_use ID。
            tool_arguments: 工具调用参数。
            reason: 拦截原因（展示给用户）。
            irreversible: 是否为不可逆操作。
            agent_state_snapshot: AgentState 快照（JSONB）。
            tenant_id: 多租户预留。

        Returns:
            ToolApproval ORM 实例。
        """
        approval = ToolApproval(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            tool_arguments=tool_arguments,
            reason=reason,
            irreversible=irreversible,
            agent_state_snapshot=agent_state_snapshot,
            status="pending",
            expire_at=datetime.now(timezone.utc) + timedelta(seconds=_APPROVAL_TTL_SECONDS),
            tenant_id=tenant_id,
        )
        self.db.add(approval)
        await self.db.flush()
        log.info(
            "approval.created",
            approval_id=str(approval.id),
            tool=tool_name,
            session_id=session_id,
        )
        return approval

    # ------------------------------------------------------------------
    # 审批操作
    # ------------------------------------------------------------------

    async def approve(
        self,
        approval_id: uuid.UUID,
        user: User,
    ) -> ToolApproval:
        """批准审批 — 用户同意执行危险工具。

        批准后将工具标记为会话级已确认，后续同一会话中再次调用同款工具不再拦截。

        Args:
            approval_id: 审批 ID。
            user: 当前用户（用于权限校验）。

        Returns:
            更新后的 ToolApproval。

        Raises:
            ValueError: 审批不存在/已处理/已过期/不属于当前用户。
        """
        approval = await self._get_and_validate(approval_id, user)

        stmt = (
            update(ToolApproval)
            .where(ToolApproval.id == approval_id)
        )
        stmt = apply_tenant_filter(stmt, ToolApproval, self._tenant_id)
        stmt = stmt.values(
            status="approved",
            resolved_at=datetime.now(timezone.utc),
            resolved_by=user.id,
        )
        await self.db.execute(stmt)
        await self.db.flush()

        # 会话级缓存 — 标记该工具为已确认
        session_id = approval.session_id
        if session_id not in self._session_confirmed:
            self._session_confirmed[session_id] = set()
        self._session_confirmed[session_id].add(approval.tool_name)

        log.info(
            "approval.approved",
            approval_id=str(approval_id),
            tool=approval.tool_name,
            session_id=session_id,
        )
        return approval

    async def reject(
        self,
        approval_id: uuid.UUID,
        user: User,
    ) -> ToolApproval:
        """拒绝审批 — 用户拒绝执行危险工具。

        Args:
            approval_id: 审批 ID。
            user: 当前用户。

        Returns:
            更新后的 ToolApproval。

        Raises:
            ValueError: 审批不存在/已处理/已过期/不属于当前用户。
        """
        approval = await self._get_and_validate(approval_id, user)

        stmt = (
            update(ToolApproval)
            .where(ToolApproval.id == approval_id)
        )
        stmt = apply_tenant_filter(stmt, ToolApproval, self._tenant_id)
        stmt = stmt.values(
            status="rejected",
            resolved_at=datetime.now(timezone.utc),
            resolved_by=user.id,
        )
        await self.db.execute(stmt)
        await self.db.flush()

        log.info(
            "approval.rejected",
            approval_id=str(approval_id),
            tool=approval.tool_name,
            session_id=approval.session_id,
        )
        return approval

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def get_pending_approvals(
        self,
        user: User,
        session_id: str | None = None,
    ) -> list[ToolApproval]:
        """查询用户的待审批列表。

        Args:
            user: 当前用户。
            session_id: 可选，按会话过滤。

        Returns:
            pending 状态的审批列表（按创建时间倒序）。
        """
        stmt = (
            select(ToolApproval)
            .where(
                ToolApproval.user_id == user.id,
                ToolApproval.status == "pending",
            )
        )
        stmt = apply_tenant_filter(stmt, ToolApproval, self._tenant_id)
        stmt = stmt.order_by(ToolApproval.created_at.desc())
        if session_id:
            stmt = stmt.where(ToolApproval.session_id == session_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_approval_by_id(
        self, approval_id: uuid.UUID
    ) -> ToolApproval | None:
        """按 ID 查询审批记录。"""
        stmt = select(ToolApproval).where(ToolApproval.id == approval_id)
        stmt = apply_tenant_filter(stmt, ToolApproval, self._tenant_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_agent_state_snapshot(
        self, approval_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """获取审批记录中的 AgentState 快照 — 恢复 Agent Loop 时调用。"""
        approval = await self.get_approval_by_id(approval_id)
        if approval is None:
            return None
        return approval.agent_state_snapshot

    # ------------------------------------------------------------------
    # 会话级确认缓存
    # ------------------------------------------------------------------

    def is_session_confirmed(self, session_id: str, tool_name: str) -> bool:
        """检查工具在当前会话中是否已确认（内存缓存，O(1)）。

        引擎在 _execute_tool_use 中调用此方法快速判断是否放行，
        避免每次工具调用都查 DB。
        """
        return tool_name in self._session_confirmed.get(session_id, set())

    def confirm_session_tool(self, session_id: str, tool_name: str) -> None:
        """手动标记会话级确认（不经过 DB 审批流程时使用）。"""
        if session_id not in self._session_confirmed:
            self._session_confirmed[session_id] = set()
        self._session_confirmed[session_id].add(tool_name)

    def clear_session(self, session_id: str) -> None:
        """清理会话确认缓存 — 对话结束时调用。"""
        self._session_confirmed.pop(session_id, None)
        log.info("approval.session_cleared", session_id=session_id)

    # ------------------------------------------------------------------
    # 服务重启恢复
    # ------------------------------------------------------------------

    async def restore_pending_approvals(self) -> int:
        """服务重启时恢复未决审批 — 标记过期审批 + 加载活跃审批到缓存。

        Returns:
            恢复的活跃审批数量。
        """
        now = datetime.now(timezone.utc)

        # 1. 标记已过期的 pending 审批为 expired
        expire_stmt = (
            update(ToolApproval)
            .where(
                ToolApproval.status == "pending",
                ToolApproval.expire_at < now,
            )
        )
        expire_stmt = apply_tenant_filter(expire_stmt, ToolApproval, self._tenant_id)
        expire_stmt = expire_stmt.values(status="expired")
        await self.db.execute(expire_stmt)
        await self.db.flush()

        # 2. 查询仍活跃的 pending 审批
        active_stmt = select(ToolApproval).where(
            ToolApproval.status == "pending",
            ToolApproval.expire_at >= now,
        )
        active_stmt = apply_tenant_filter(active_stmt, ToolApproval, self._tenant_id)
        result = await self.db.execute(active_stmt)
        active = list(result.scalars().all())

        # 3. 加载到会话级缓存
        for approval in active:
            if approval.session_id not in self._session_confirmed:
                self._session_confirmed[approval.session_id] = set()
            # 注意：pending 审批不加入 confirmed set，仅记录存在
            # confirmed set 只在 approve 后才加入

        log.info(
            "approval.restored",
            active_count=len(active),
            expired_marked=True,
        )
        return len(active)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _get_and_validate(
        self, approval_id: uuid.UUID, user: User
    ) -> ToolApproval:
        """获取并校验审批记录 — 检查存在性/状态/权限。"""
        approval = await self.get_approval_by_id(approval_id)
        if approval is None:
            raise ValueError(f"审批记录不存在: {approval_id}")
        if approval.user_id != user.id:
            raise ValueError("无权操作此审批记录")
        if approval.status != "pending":
            raise ValueError(f"审批已处理（当前状态: {approval.status}）")
        if approval.expire_at and approval.expire_at < datetime.now(timezone.utc):
            raise ValueError("审批已过期")
        return approval
