"""
租户服务 — 管理租户模块配置、套餐信息。

单一职责：封装 Tenant.settings JSONB 的读写逻辑，
使 API 层和依赖注入层不直接操作 JSONB 结构。

核心概念：
    - 基础模块（is_basic=True）永远启用，不受 settings 控制
    - 可选模块通过 settings.enabled_modules 列表开关
    - 未初始化 settings 时按套餐默认（PLAN_DEFAULTS）填充
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.modules import (
    BASIC_MODULE_IDS,
    MODULE_IDS,
    PLAN_DEFAULTS,
    MODULE_REGISTRY,
    merge_with_basics,
)
from app.models.billing import Tenant


class TenantService:
    """租户模块管理服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 租户查询
    # ------------------------------------------------------------------

    async def get_tenant(
        self, tenant_id: uuid.UUID | None = None
    ) -> Tenant | None:
        """获取租户实体。

        Args:
            tenant_id: 租户 ID。为 None 时取第一条活跃租户
                       （私有部署单租户场景）。

        Returns:
            Tenant 实例或 None。
        """
        if tenant_id is not None:
            stmt = select(Tenant).where(
                Tenant.id == tenant_id,
                Tenant.deleted_at.is_(None),
            )
        else:
            stmt = (
                select(Tenant)
                .where(Tenant.deleted_at.is_(None))
                .limit(1)
            )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # 模块查询
    # ------------------------------------------------------------------

    async def get_enabled_modules(
        self, tenant_id: uuid.UUID | None = None
    ) -> list[str]:
        """获取租户已启用的模块列表。

        优先级：
        1. settings.enabled_modules（显式配置）
        2. PLAN_DEFAULTS[plan]（套餐默认）
        3. BASIC_MODULE_IDS（兜底）
        """
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            return sorted(BASIC_MODULE_IDS)

        settings = tenant.settings or {}
        enabled = settings.get("enabled_modules")

        if enabled is not None and isinstance(enabled, list):
            # 基础模块永远包含
            return merge_with_basics(enabled)

        # 未显式配置，按套餐默认填充
        return PLAN_DEFAULTS.get(tenant.plan, sorted(BASIC_MODULE_IDS))

    async def is_module_enabled(
        self,
        module_name: str,
        tenant_id: uuid.UUID | None = None,
    ) -> bool:
        """检查租户是否启用了指定模块。

        基础模块永远返回 True（不受 settings 控制）。
        """
        # 基础模块永远启用
        if module_name in BASIC_MODULE_IDS:
            return True

        enabled = await self.get_enabled_modules(tenant_id)
        return module_name in enabled

    async def list_modules_with_status(
        self, tenant_id: uuid.UUID | None = None
    ) -> list[dict[str, Any]]:
        """获取所有模块及其在当前租户下的启用状态。

        Returns:
            模块信息列表，每项包含 id/name/description/category/is_basic/enabled。
        """
        enabled = await self.get_enabled_modules(tenant_id)
        return [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "category": m.category,
                "is_basic": m.is_basic,
                "enabled": m.is_basic or m.id in enabled,
            }
            for m in MODULE_REGISTRY
        ]

    # ------------------------------------------------------------------
    # 模块更新
    # ------------------------------------------------------------------

    async def update_enabled_modules(
        self,
        module_ids: list[str],
        tenant_id: uuid.UUID | None = None,
    ) -> list[str]:
        """更新租户启用的模块列表。

        规则：
        - 所有模块 ID 必须在 MODULE_REGISTRY 中注册
        - 基础模块永远包含（即使用户未传入也会自动补上）
        - 写入 settings.enabled_modules（JSONB）

        Args:
            module_ids: 要启用的模块 ID 列表（不含基础模块也可以，会自动补齐）。
            tenant_id: 租户 ID。

        Returns:
            更新后实际启用的模块列表（已排序）。

        Raises:
            ValueError: 模块 ID 无效或租户不存在。
        """
        # 验证模块 ID
        invalid = set(module_ids) - MODULE_IDS
        if invalid:
            raise ValueError(f"未知的模块 ID: {sorted(invalid)}")

        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            raise ValueError("租户不存在")

        # 合并基础模块
        final = merge_with_basics(module_ids)

        # 写入 settings JSONB
        settings = tenant.settings or {}
        settings["enabled_modules"] = final
        tenant.settings = settings

        await self._db.flush()
        return final

    async def toggle_module(
        self,
        module_id: str,
        enabled: bool,
        tenant_id: uuid.UUID | None = None,
    ) -> list[str]:
        """开关单个模块。

        基础模块不可关闭（忽略 enabled=False）。

        Args:
            module_id: 模块 ID。
            enabled: True 启用 / False 禁用。
            tenant_id: 租户 ID。

        Returns:
            更新后启用的模块列表。

        Raises:
            ValueError: 模块 ID 无效或租户不存在。
        """
        if module_id not in MODULE_IDS:
            raise ValueError(f"未知的模块 ID: {module_id}")

        current = await self.get_enabled_modules(tenant_id)
        current_set = set(current)

        if enabled:
            current_set.add(module_id)
        else:
            # 基础模块不可关闭
            if module_id in BASIC_MODULE_IDS:
                pass  # 静默忽略
            else:
                current_set.discard(module_id)

        return await self.update_enabled_modules(
            sorted(current_set), tenant_id
        )
