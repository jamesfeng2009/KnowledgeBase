"""
API 密钥仓储 — 单一职责：API 密钥领域的数据访问。

遵循单一职责：ApiKeyRepository 只处理 api_keys 表的查询，
不涉及密钥生成、哈希计算等安全逻辑（由 ApiKeyService 处理）。

遵循开闭原则：继承 BaseRepository 获得标准 CRUD，
扩展添加密钥专属查询（按前缀查询、停用、更新最后使用时间）。

注意：ApiKey 模型不支持软删除（无 SoftDeleteMixin），
使用 is_active 字段标记停用状态，所有查询不包含 deleted_at 过滤。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.apikey import ApiKey
from app.repositories.base import BaseRepository


class ApiKeyRepository(BaseRepository[ApiKey]):
    """API 密钥仓储 — 封装 api_keys 表的领域查询。

    ApiKey 模型不支持软删除，使用 is_active 字段标记停用状态。
    """

    def __init__(self, session: AsyncSession, tenant_id: UUID | None = None) -> None:
        super().__init__(ApiKey, session, tenant_id=tenant_id)

    async def get_by_prefix(self, key_prefix: str) -> ApiKey | None:
        """根据密钥前缀查询（用于鉴权时快速定位记录）。

        密钥前缀为明文前 8 位，在创建时写入。
        鉴权流程：先通过前缀定位记录，再校验完整哈希。

        Args:
            key_prefix: 密钥前缀（明文前 8 位）。

        Returns:
            匹配的 ApiKey 实例，未找到返回 None。
        """
        stmt = select(ApiKey).where(
            ApiKey.key_prefix == key_prefix,
            ApiKey.is_active.is_(True),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_all(self, is_active: bool | None = None) -> list[ApiKey]:
        """查询所有 API 密钥（可按启用状态过滤）。

        Args:
            is_active: 启用状态过滤，None 表示不过滤。

        Returns:
            ApiKey 列表（按创建时间倒序）。
        """
        stmt = select(ApiKey)
        if is_active is not None:
            stmt = stmt.where(ApiKey.is_active.is_(is_active))
        stmt = self._apply_all_filters(stmt)
        stmt = stmt.order_by(ApiKey.created_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_last_used(self, id: UUID) -> None:
        """更新密钥最后使用时间为当前时间。

        使用 UPDATE 语句直接更新，无需先查询再修改，减少一次数据库往返。

        Args:
            id: 密钥 ID。
        """
        stmt = update(ApiKey).where(ApiKey.id == id)
        if self._tenant_id is not None:
            stmt = stmt.where(ApiKey.tenant_id == self._tenant_id)
        stmt = stmt.values(last_used_at=datetime.now(timezone.utc))
        await self.session.execute(stmt)
        await self.session.flush()

    async def deactivate(self, id: UUID) -> bool:
        """停用密钥（软停用，不物理删除）。

        将 is_active 设为 False。已停用的密钥在鉴权时直接拒绝。

        Args:
            id: 密钥 ID。

        Returns:
            True 表示停用成功，False 表示密钥不存在。
        """
        stmt = update(ApiKey).where(ApiKey.id == id)
        if self._tenant_id is not None:
            stmt = stmt.where(ApiKey.tenant_id == self._tenant_id)
        stmt = stmt.values(is_active=False)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0
