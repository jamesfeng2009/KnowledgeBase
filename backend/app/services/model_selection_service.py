"""P2 模型选择服务 — 单一职责：管理用户会话级模型选择。

两级优先级（简化设计，不引入 user default 层）：
    1. session 级（DB 持久化）— 用户为该会话明确选择的模型
    2. system 默认 — models.json 中 is_default=True 的模型

核心方法：
    - get_session_model(user_id, session_id) → 查询会话级模型选择
    - set_session_model(user_id, session_id, model_id) → 设置/更新会话级模型
    - resolve_model(user_id, session_id) → 两级优先级解析，返回最终模型 ID

遵循单一职责：本服务只管理模型选择逻辑，不创建 Provider 实例。
遵循开闭原则：新增模型类型只需在 models.json 中添加，无需修改本服务。
"""
from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.model_config import (
    get_available_models,
    get_default_model,
    get_model_by_id,
)
from app.models.user_model_preference import UserModelPreference
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)


class ModelSelectionService:
    """P2 模型选择服务 — 管理会话级模型选择与两级优先级解析。

    使用方式::

        service = ModelSelectionService(db)
        # 设置会话模型
        await service.set_session_model(user_id, session_id, "claude-haiku-4")
        # 解析最终模型（session > system default）
        model_id = await service.resolve_model(user_id, session_id)
    """

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get_session_model(
        self, user_id: uuid.UUID, session_id: str
    ) -> str | None:
        """查询会话级模型选择。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。

        Returns:
            模型 ID（如 "claude-sonnet-4.6"），未设置时返回 None。
        """
        stmt = select(UserModelPreference.model_id).where(
            UserModelPreference.user_id == user_id,
            UserModelPreference.session_id == session_id,
        )
        stmt = apply_tenant_filter(stmt, UserModelPreference, self._tenant_id)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def set_session_model(
        self,
        user_id: uuid.UUID,
        session_id: str,
        model_id: str,
        tenant_id: uuid.UUID | None = None,
    ) -> UserModelPreference:
        """设置/更新会话级模型选择（upsert 语义）。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。
            model_id: 模型 ID（必须在 models.json 中存在且属于当前 deploy_mode）。
            tenant_id: 多租户预留。

        Returns:
            创建或更新后的 UserModelPreference 记录。

        Raises:
            ValueError: model_id 不存在或不属于当前部署模式。
        """
        # 校验模型 ID 有效性
        model = get_model_by_id(model_id)
        if model is None:
            raise ValueError(f"模型 ID 不存在: {model_id}")

        from app.config import get_settings

        settings = get_settings()
        if model.get("deploy_mode") != settings.DEPLOY_MODE:
            raise ValueError(
                f"模型 {model_id} 不属于当前部署模式 {settings.DEPLOY_MODE}"
            )

        if not model.get("enabled", True):
            raise ValueError(f"模型 {model_id} 已禁用")

        # 查询是否已有记录
        stmt = select(UserModelPreference).where(
            UserModelPreference.user_id == user_id,
            UserModelPreference.session_id == session_id,
        )
        stmt = apply_tenant_filter(stmt, UserModelPreference, self._tenant_id)
        result = await self._db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            # 更新
            existing.model_id = model_id
            if tenant_id is not None:
                existing.tenant_id = tenant_id
            await self._db.flush()
            log.info(
                "model_selection.updated",
                user_id=str(user_id),
                session_id=session_id,
                model_id=model_id,
            )
            return existing

        # 新建
        pref = UserModelPreference(
            user_id=user_id,
            session_id=session_id,
            model_id=model_id,
            tenant_id=tenant_id,
        )
        self._db.add(pref)
        await self._db.flush()
        log.info(
            "model_selection.created",
            user_id=str(user_id),
            session_id=session_id,
            model_id=model_id,
        )
        return pref

    async def resolve_model(
        self, user_id: uuid.UUID, session_id: str
    ) -> str:
        """两级优先级解析 — 返回最终使用的模型 ID。

        优先级：
            1. session 级（DB 持久化）— 用户为该会话选择的模型
            2. system 默认 — models.json 中 is_default=True 的模型

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。

        Returns:
            最终模型 ID。如果 session 级未设置或已失效，回退到 system 默认。
        """
        # 1. 尝试 session 级
        session_model_id = await self.get_session_model(user_id, session_id)
        if session_model_id is not None:
            # 校验 session 级模型是否仍然有效（可能被禁用或删除）
            from app.llm.model_config import get_model_config_for_user

            model = get_model_config_for_user(session_model_id)
            if model is not None:
                return session_model_id
            # session 级模型已失效 — 回退到默认
            log.warning(
                "model_selection.session_model_invalid",
                session_model_id=session_model_id,
                session_id=session_id,
            )

        # 2. 回退到 system 默认
        default_model = get_default_model()
        if default_model is not None:
            return default_model["id"]

        # 兜底 — 不应该发生，models.json 至少有一个模型
        log.error("model_selection.no_default_model")
        return ""

    async def get_available_models_for_user(
        self, user_id: uuid.UUID | None = None
    ) -> list[dict]:
        """获取当前部署模式下所有可用模型（前端下拉框数据源）。

        Args:
            user_id: 可选，传入时标记当前选中模型。

        Returns:
            模型列表，每项含 id/display_name/description/tier/is_default。
        """
        models = get_available_models()
        result = []
        for m in models:
            result.append(
                {
                    "id": m.get("id", ""),
                    "display_name": m.get("display_name", ""),
                    "description": m.get("description", ""),
                    "tier": m.get("tier", "standard"),
                    "is_default": m.get("is_default", False),
                    "max_tokens": m.get("max_tokens", 0),
                    "supports_vision": m.get("supports_vision", False),
                    "supports_tool_use": m.get("supports_tool_use", False),
                }
            )
        return result
