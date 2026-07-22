"""
Repository 基类 — 单一职责：提供泛型 CRUD 数据访问。

遵循开闭原则：子类 Repository 继承 BaseRepository 即可获得标准 CRUD 能力，
通过扩展添加领域专属查询方法，无需修改基类。

遵循单一职责：BaseRepository 只负责通用数据访问（增删改查 + 软删除 + 计数），
不包含任何业务逻辑。

软删除策略：
- 所有查询自动过滤 deleted_at IS NULL（仅当模型支持软删除时）。
- soft_delete 方法将 deleted_at 设为当前时间，不物理删除记录。
- 不支持软删除的模型（如 Message、Feedback）调用 soft_delete 返回 False。

多租户隔离（P0）：
- 构造函数接受可选的 tenant_id 参数。
- _apply_tenant_filter 自动为查询追加 WHERE tenant_id = :tid 条件。
- create 方法自动写入 tenant_id（如未显式传入）。
- tenant_id=None 时不过滤（单租户兜底场景）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

# 泛型类型变量，绑定到 SQLAlchemy DeclarativeBase
ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """泛型仓储基类 — 提供标准 CRUD 操作。

    使用方式（子类示例）：

        class UserRepository(BaseRepository[User]):
            def __init__(self, session: AsyncSession, tenant_id: UUID | None = None):
                super().__init__(User, session, tenant_id=tenant_id)

            async def get_by_email(self, email: str) -> User | None:
                ...

    所有查询方法自动排除已软删除的记录（deleted_at IS NULL）。
    当 tenant_id 不为 None 时，所有查询自动追加租户过滤条件。
    """

    def __init__(
        self,
        model: type[ModelT],
        session: AsyncSession,
        tenant_id: UUID | None = None,
    ) -> None:
        """初始化仓储。

        Args:
            model: ORM 模型类（如 User、Document）。
            session: 异步数据库会话，由依赖注入 get_db_session 提供。
            tenant_id: 租户 ID，不为 None 时所有查询自动过滤租户。
        """
        self.model: type[ModelT] = model
        self.session: AsyncSession = session
        self._tenant_id: UUID | None = tenant_id

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _apply_soft_delete_filter(self, stmt):
        """为 SELECT 语句追加软删除过滤条件。

        仅当模型定义了 deleted_at 列（即继承了 SoftDeleteMixin）时生效，
        否则原样返回语句，保证对非软删除模型（Message、Feedback 等）的兼容。
        """
        if hasattr(self.model, "deleted_at"):
            return stmt.where(self.model.deleted_at.is_(None))
        return stmt

    def _apply_tenant_filter(self, stmt):
        """为 SELECT 语句追加租户隔离过滤条件。

        - 当 ``self._tenant_id`` 为 None 时，不过滤（单租户兜底场景）。
        - 当模型没有 tenant_id 列时，不过滤。
        - 否则追加 ``WHERE tenant_id = :tid`` 条件。

        子类如有特殊隔离需求（如 user_repository 的 get_by_email 需跨租户
        查唯一邮箱），可覆盖此方法。
        """
        if self._tenant_id is None:
            return stmt
        if not hasattr(self.model, "tenant_id"):
            return stmt
        return stmt.where(self.model.tenant_id == self._tenant_id)

    def _apply_all_filters(self, stmt):
        """同时应用软删除过滤和租户过滤。"""
        stmt = self._apply_soft_delete_filter(stmt)
        stmt = self._apply_tenant_filter(stmt)
        return stmt

    # ------------------------------------------------------------------
    # 标准 CRUD
    # ------------------------------------------------------------------

    async def get_by_id(self, id: UUID) -> ModelT | None:
        """根据主键查询单条记录（排除已软删除 + 租户过滤）。"""
        stmt = select(self.model).where(self.model.id == id)
        stmt = self._apply_all_filters(stmt)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_all(self, skip: int = 0, limit: int = 20) -> list[ModelT]:
        """分页查询全部记录（排除已软删除 + 租户过滤）。

        Args:
            skip: 跳过的记录数（OFFSET）。
            limit: 返回的最大记录数（LIMIT）。
        """
        stmt = select(self.model)
        stmt = self._apply_all_filters(stmt)
        stmt = stmt.offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create(self, **kwargs) -> ModelT:
        """创建一条新记录。

        - 自动注入 tenant_id（如模型有该字段且未显式传入）。
        - 使用 flush 将 INSERT 发送到数据库（不提交），
          随后 refresh 加载服务端生成的默认值（如 created_at、updated_at）。
        - 事务提交由 get_db_session 依赖统一处理。
        """
        if self._tenant_id is not None and hasattr(self.model, "tenant_id"):
            kwargs.setdefault("tenant_id", self._tenant_id)
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def update(self, id: UUID, **kwargs) -> ModelT | None:
        """根据主键更新记录（排除已软删除 + 租户过滤）。

        采用"先查询再修改"策略：
        - 确保记录存在且未被软删除；
        - 确保记录属于当前租户（租户隔离）；
        - 利用 SQLAlchemy 会话的变更跟踪自动生成 UPDATE 语句；
        - 记录不存在时返回 None。
        """
        instance = await self.get_by_id(id)
        if instance is None:
            return None
        for key, value in kwargs.items():
            setattr(instance, key, value)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def soft_delete(self, id: UUID) -> bool:
        """软删除：将 deleted_at 设为当前时间。

        - 仅对支持软删除的模型生效（继承了 SoftDeleteMixin）；
        - 不支持软删除的模型返回 False；
        - 记录不存在或已删除时返回 False；
        - 受租户过滤保护（不能删除其他租户的记录）。
        """
        if not hasattr(self.model, "deleted_at"):
            return False
        instance = await self.get_by_id(id)
        if instance is None:
            return False
        instance.deleted_at = datetime.now(timezone.utc)
        await self.session.flush()
        return True

    async def count(self) -> int:
        """统计未软删除 + 租户过滤后的记录总数。"""
        stmt = select(func.count()).select_from(self.model)
        if hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))
        stmt = self._apply_tenant_filter(stmt)
        result = await self.session.scalar(stmt)
        return int(result) if result is not None else 0
