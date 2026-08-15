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
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

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

    async def check_write(self, kb_id: UUID) -> bool:
        """检查当前用户对指定知识库是否具有写权限（上传/编辑/删除文档）。

        与 ``check_function``（读权限）的区别：
        - 全局 admin、知识库 owner 直接放行；
        - 成员要求 ``KbMember.role in ("admin", "editor")`` ——
          viewer（只读成员）不得写入，否则角色权限模型失效。

        Args:
            kb_id: 知识库 ID。

        Returns:
            True 表示可写，False 表示无写权限或知识库不存在。
        """
        if self.user.role == "admin":
            return True

        kb_stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.deleted_at.is_(None),
        )
        kb_stmt = apply_tenant_filter(kb_stmt, KnowledgeBase, self._tenant_id)
        kb_result = await self.db.execute(kb_stmt)
        kb = kb_result.scalars().first()
        if kb is None:
            return False

        if kb.owner_id == self.user.id:
            return True

        member_stmt = select(KbMember).where(
            KbMember.kb_id == kb_id,
            KbMember.user_id == self.user.id,
            KbMember.role.in_(("admin", "editor")),
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

    # ------------------------------------------------------------------
    # 检索结果过滤（RAG 引擎 dict 候选）
    # ------------------------------------------------------------------

    async def get_accessible_kb_ids(self) -> set[UUID] | None:
        """返回当前用户可访问的知识库 ID 集合。

        用于 RAG 检索层下推过滤（OpenSearch terms filter / 向量检索 kb_ids），
        在召回阶段就限定知识库范围，避免越权文档进入重排与生成上下文。

        Returns:
            - admin：返回 None（表示不限制，检索全部知识库）；
            - 普通用户：返回可访问的 kb_id 集合（可能为空集合，
              空集合表示无任何可访问知识库，检索应短路返回空结果）。
        """
        if self.user.role == "admin":
            return None

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
        return {row[0] for row in result.all()}

    async def filter_retrieval_candidates(
        self,
        candidates: list[dict],
    ) -> list[dict]:
        """过滤 RAG 检索返回的 dict 候选 — Final Gate 三项复检。

        检索不变量（app/rag/retrieval_invariants.py，DB 为权威数据源）：
            I1_PUBLISHED — 文档真实状态必须为 published；
            I3_CLEARANCE — 文档密级 ≤ 用户密级；
            I2/I4 — 知识库归属过滤（含租户隔离，apply_tenant_filter）。

        与 kb_ids 下推过滤的关系：kb_ids 下推解决"知识库归属"维度（召回层），
        本方法在重排前以 DB 真实状态做最终裁决，二者构成双重保障。
        即使下推过滤被绕过、索引数据越权写入、或向量库中残留了文档
        转 draft 前的旧向量，本方法仍按 DB 拦截（fail-closed）。

        admin 放行 kb / 密级维度（与 filter_documents 语义一致），但
        I1 状态复检对 admin 同样生效 — 半成品不进生成上下文与角色无关。

        Args:
            candidates: 检索候选 dict 列表，每项含 ``doc_id`` / ``kb_id`` /
                ``content`` / ``score`` 等字段（HybridRetriever 返回格式）。

        Returns:
            过滤后的候选子集（保持原顺序）。
        """
        if not candidates:
            return candidates

        # 批量查 DB 真实状态 + 密级 — 状态查不到（文档已删除 / doc_id 非法）
        # 同样剔除，与密级缺失的保守策略一致。
        doc_meta_map = await self._load_doc_meta(candidates)

        # I1 复检（全部角色）：非 published 一律剔除
        published_only = [
            c for c in candidates
            if doc_meta_map.get(str(c.get("doc_id")), {}).get("status") == "published"
        ]
        dropped_status = len(candidates) - len(published_only)
        if dropped_status:
            log.warning(
                "permission.doc_status_blocked",
                dropped=dropped_status,
                examples=[
                    str(c.get("doc_id")) for c in candidates
                    if doc_meta_map.get(str(c.get("doc_id")), {}).get("status") != "published"
                ][:5],
            )

        # admin 放行 kb / 密级维度（I1 已复检完毕）
        if self.user.role == "admin":
            return published_only
        # I1 后已无候选（全 draft / DB 查不到）— 短路，省去 kb 集合查询
        if not published_only:
            return []

        accessible_kb_ids = await self.get_accessible_kb_ids()
        if accessible_kb_ids is None:
            return published_only
        if not accessible_kb_ids:
            return []

        accessible_strs = {str(kb_id) for kb_id in accessible_kb_ids}
        user_level = _CLEARANCE_ORDER.get(self.user.clearance_level, 1)

        # I4 复检：知识库归属（kb_id 缺失的候选保守剔除 — 无法确认归属不放行）
        kb_allowed = [
            c for c in published_only
            if c.get("kb_id") and str(c["kb_id"]) in accessible_strs
        ]
        if not kb_allowed:
            return []

        # I3 复检：密级过滤 — 使用 _load_doc_meta 已查出的 classification
        filtered: list[dict] = []
        for c in kb_allowed:
            classification = doc_meta_map.get(str(c.get("doc_id")), {}).get("classification")
            if classification is None:
                log.warning(
                    "permission.classification_missing",
                    doc_id=str(c.get("doc_id")),
                )
                continue
            if _CLEARANCE_ORDER.get(classification, 1) <= user_level:
                filtered.append(c)
        return filtered

    async def _load_doc_meta(
        self,
        candidates: list[dict],
    ) -> dict[str, dict[str, str | None]]:
        """批量查询候选文档的 DB 真实状态与密级。

        Returns:
            {doc_id: {"status": ..., "classification": ...}} — 仅含 DB 中
            存在的文档；查不到的 doc_id 不出现在结果中（fail-closed）。
        """
        doc_ids = {c.get("doc_id") for c in candidates if c.get("doc_id")}
        if not doc_ids:
            return {}

        from uuid import UUID as _UUID

        valid_uuids: list[_UUID] = []
        for did in doc_ids:
            try:
                valid_uuids.append(_UUID(str(did)))
            except (ValueError, TypeError):
                continue
        if not valid_uuids:
            return {}

        stmt = select(Document.id, Document.classification, Document.status).where(
            Document.id.in_(valid_uuids)
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        rows = (await self.db.execute(stmt)).all()
        return {
            str(row[0]): {"status": row[2], "classification": row[1]}
            for row in rows
        }
