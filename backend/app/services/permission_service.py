"""
权限服务 — 单一职责：基于 ABAC 模型进行功能权限校验与文档过滤。

遵循单一职责：本模块只做权限判断（能否访问知识库、能否查看文档），
不涉及任何业务创建 / 修改 / 删除逻辑。

遵循开闭原则：权限策略由数据驱动（角色 + 密级 + 成员关系），
新增资源类型只需在 check_function 中追加分支，不修改既有判断路径。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Document, KnowledgeBase
from app.models.user import KbMember, User
from app.utils.tenant import apply_tenant_filter

# 密级权重表 — 数字越大密级越高。
# 用户只能访问 classification 权重 <= 自身 clearance_level 权重的文档。
_CLEARANCE_ORDER: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "secret": 3,
}


class PermissionService:
    """权限服务 — 封装 ABAC 权限校验与文档过滤。

    通过依赖注入接收数据库会话与当前用户，所有方法均围绕"当前用户能做什么"展开。
    """

    def __init__(
        self, db: AsyncSession, user: User, tenant_id: UUID | None = None
    ) -> None:
        """初始化权限服务。

        Args:
            db: 异步数据库会话。
            user: 当前请求的已认证用户。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: User = user
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 密级辅助
    # ------------------------------------------------------------------

    def allowed_classifications(self) -> list[str]:
        """返回当前用户密级可访问的所有文档密级列表。

        用于在分页查询中构建 ``classification IN (...)`` 条件，
        将密级过滤下推到 SQL 层，保证分页 total 计数准确。

        admin 角色不受密级限制（返回全部密级）。
        """
        if self.user.role == "admin":
            return list(_CLEARANCE_ORDER.keys())
        user_level = _CLEARANCE_ORDER.get(self.user.clearance_level, 1)
        return [name for name, level in _CLEARANCE_ORDER.items() if level <= user_level]

    # ------------------------------------------------------------------
    # 功能权限校验
    # ------------------------------------------------------------------

    async def check_function(self, kb_id: UUID) -> bool:
        """检查当前用户是否可访问指定知识库。

        可访问条件（OR 逻辑）：
        1. 用户角色为 admin（全局管理员，放行所有知识库）；
        2. 用户是知识库的所有者（owner_id）；
        3. 用户是知识库的成员（kb_members 关联表）。

        知识库不存在或已软删除时返回 False。

        Args:
            kb_id: 知识库 ID。

        Returns:
            True 表示可访问，False 表示无权访问或知识库不存在。
        """
        # 全局管理员放行
        if self.user.role == "admin":
            return True

        # 查询知识库是否存在且未删除
        kb_stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.deleted_at.is_(None),
        )
        kb_stmt = apply_tenant_filter(kb_stmt, KnowledgeBase, self._tenant_id)
        kb_result = await self.db.execute(kb_stmt)
        kb = kb_result.scalars().first()
        if kb is None:
            return False

        # 所有者可直接访问
        if kb.owner_id == self.user.id:
            return True

        # 成员关系校验
        member_stmt = select(KbMember).where(
            KbMember.kb_id == kb_id,
            KbMember.user_id == self.user.id,
        )
        member_stmt = apply_tenant_filter(member_stmt, KbMember, self._tenant_id)
        member_result = await self.db.execute(member_stmt)
        return member_result.scalars().first() is not None

    # ------------------------------------------------------------------
    # 文档过滤
    # ------------------------------------------------------------------

    async def filter_documents(self, documents: list[Document]) -> list[Document]:
        """过滤出当前用户可访问的文档列表。

        过滤维度：
        1. 文档所属知识库对用户可见（所有者 / 成员 / admin）；
        2. 文档密级不超过用户密级 clearance_level。

        适用于检索结果、推荐列表等内存中文档集合的后置过滤。
        分页查询场景请使用 allowed_classifications() 将密级条件下推到 SQL。

        Args:
            documents: 待过滤的文档列表（通常来自检索 / 推荐引擎）。

        Returns:
            过滤后用户可见的文档子集。
        """
        user_level = _CLEARANCE_ORDER.get(self.user.clearance_level, 1)

        # admin 可访问所有文档（统一口径：admin 放行所有密级，
        # 与 check_function 的 admin 语义一致）
        if self.user.role == "admin":
            return documents

        # 普通用户：先查出可访问的知识库 ID 集合
        member_subq = select(KbMember.kb_id).where(KbMember.user_id == self.user.id)
        member_subq = apply_tenant_filter(member_subq, KbMember, self._tenant_id)
        accessible_stmt = (
            select(KnowledgeBase.id)
            .where(
                KnowledgeBase.deleted_at.is_(None),
                or_(
                    KnowledgeBase.owner_id == self.user.id,
                    KnowledgeBase.id.in_(member_subq),
                ),
            )
        )
        accessible_stmt = apply_tenant_filter(accessible_stmt, KnowledgeBase, self._tenant_id)
        result = await self.db.execute(accessible_stmt)
        accessible_kb_ids: set[UUID] = {row[0] for row in result.all()}

        # 按知识库归属 + 密级双重过滤
        return [
            doc
            for doc in documents
            if doc.kb_id in accessible_kb_ids
            and _CLEARANCE_ORDER.get(doc.classification, 1) <= user_level
        ]
