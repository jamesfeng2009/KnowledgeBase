"""
用户仓储 — 单一职责：用户领域的数据访问。

遵循单一职责：UserRepository 只处理 User 表的查询，
不涉及知识库、对话等其他领域。

遵循开闭原则：继承 BaseRepository 获得标准 CRUD，
扩展添加用户专属查询（按邮箱、按部门、按上级）。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """用户仓储 — 封装 User 表的领域查询。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(User, session)

    async def get_by_email(self, email: str) -> User | None:
        """根据邮箱查询用户（排除已软删除）。

        邮箱在数据库层有唯一约束，结果至多一条。
        """
        stmt = select(User).where(
            User.email == email,
            User.deleted_at.is_(None),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_users_by_dept(self, dept_id: UUID) -> list[User]:
        """查询指定部门下的所有用户（排除已软删除）。"""
        stmt = (
            select(User)
            .where(
                User.dept_id == dept_id,
                User.deleted_at.is_(None),
            )
            .order_by(User.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_subordinates(self, manager_id: UUID) -> list[User]:
        """查询某用户的直接下属（排除已软删除，仅返回活跃用户）。"""
        stmt = (
            select(User)
            .where(
                User.manager_id == manager_id,
                User.deleted_at.is_(None),
                User.is_active.is_(True),
            )
            .order_by(User.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
