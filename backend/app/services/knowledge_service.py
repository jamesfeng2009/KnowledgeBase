"""
知识库管理服务 — 单一职责：编排知识库与文档的业务流程。

遵循单一职责：KnowledgeService 只负责业务编排（权限校验 → 仓储调用 → 结果返回），
不直接编写 SQL，也不感知 HTTP 层细节。数据访问委托给 Repository，权限校验委托给 PermissionService。

遵循开闭原则：通过依赖注入组合 Repository 与 PermissionService，
新增知识库能力只需追加方法，不修改既有方法实现。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Document, KnowledgeBase
from app.models.user import KbMember, User
from app.repositories.knowledge_repository import (
    DocumentRepository,
    KnowledgeBaseRepository,
)
from app.services.permission_service import PermissionService
from app.utils.pagination import PageResult, PaginationParams, paginate
from app.utils.tenant import apply_tenant_filter


class KnowledgeService:
    """知识库管理服务 — 封装知识库与文档的 CRUD 业务编排。

    每个公开方法遵循统一流程：校验权限 → 调用仓储 → 返回结果。
    异常策略：资源不存在抛 ValueError，权限不足抛 PermissionError，
    由上层 API 层（FastAPI exception_handler）统一翻译为 HTTP 状态码。
    """

    def __init__(
        self, db: AsyncSession, user: User, tenant_id: UUID | None = None
    ) -> None:
        """初始化知识库服务，注入依赖。

        Args:
            db: 异步数据库会话，事务由 get_db_session 统一管理。
            user: 当前已认证用户，用于权限判定与 owner_id 回填。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: User = user
        self._tenant_id = tenant_id
        self.kb_repo: KnowledgeBaseRepository = KnowledgeBaseRepository(
            db, tenant_id=tenant_id
        )
        self.doc_repo: DocumentRepository = DocumentRepository(
            db, tenant_id=tenant_id
        )
        self.permission: PermissionService = PermissionService(
            db, user, tenant_id=tenant_id
        )

    # ------------------------------------------------------------------
    # 知识库 CRUD
    # ------------------------------------------------------------------

    async def create_kb(
        self,
        name: str,
        description: str | None,
        visibility: str = "private",
    ) -> KnowledgeBase:
        """创建知识库，当前用户自动成为所有者。

        Args:
            name: 知识库名称。
            description: 知识库描述（可选）。
            visibility: 可见性 — public / private / dept。

        Returns:
            创建后的 KnowledgeBase 实例（已 flush + refresh，含服务端默认值）。
        """
        return await self.kb_repo.create(
            name=name,
            description=description,
            visibility=visibility,
            owner_id=self.user.id,
            dept_id=self.user.dept_id,
        )

    async def update_kb(self, kb_id: UUID, **kwargs) -> KnowledgeBase:
        """更新知识库信息（仅所有者或 admin 可操作）。

        Args:
            kb_id: 知识库 ID。
            **kwargs: 待更新字段（如 name / description / visibility）。

        Returns:
            更新后的 KnowledgeBase 实例。

        Raises:
            ValueError: 知识库不存在。
            PermissionError: 当前用户无权修改该知识库。
        """
        kb = await self.kb_repo.get_by_id(kb_id)
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        if kb.owner_id != self.user.id and self.user.role != "admin":
            raise PermissionError("仅所有者或管理员可修改知识库")

        updated = await self.kb_repo.update(kb_id, **kwargs)
        if updated is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        return updated

    async def delete_kb(self, kb_id: UUID) -> None:
        """软删除知识库（仅所有者或 admin 可操作）。

        软删除不会物理移除记录，仅将 deleted_at 标记为当前时间，
        所有后续查询自动过滤已删除数据。

        Args:
            kb_id: 知识库 ID。

        Raises:
            ValueError: 知识库不存在。
            PermissionError: 当前用户无权删除该知识库。
        """
        kb = await self.kb_repo.get_by_id(kb_id)
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        if kb.owner_id != self.user.id and self.user.role != "admin":
            raise PermissionError("仅所有者或管理员可删除知识库")

        await self.kb_repo.soft_delete(kb_id)

    async def get_kb(self, kb_id: UUID) -> KnowledgeBase:
        """获取单个知识库详情（校验访问权限）。

        Args:
            kb_id: 知识库 ID。

        Returns:
            KnowledgeBase 实例。

        Raises:
            ValueError: 知识库不存在。
            PermissionError: 当前用户无权访问该知识库。
        """
        kb = await self.kb_repo.get_by_id(kb_id)
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        if not await self.permission.check_function(kb_id):
            raise PermissionError("无权访问该知识库")
        return kb

    async def list_kbs(self, page: int, size: int) -> PageResult:
        """分页查询当前用户可访问的知识库列表。

        可访问范围：所有者 / 成员 / admin（全部）。
        使用 SQL 层分页（offset/limit）保证 total 计数准确。

        Args:
            page: 页码（从 1 开始）。
            size: 每页条数（上限 100）。

        Returns:
            PageResult[KnowledgeBase]。
        """
        params = PaginationParams(page=page, size=size)

        if self.user.role == "admin":
            # admin 可见全部知识库
            stmt = select(KnowledgeBase).where(
                KnowledgeBase.deleted_at.is_(None)
            )
            stmt = apply_tenant_filter(stmt, KnowledgeBase, self._tenant_id)
            stmt = stmt.order_by(KnowledgeBase.created_at.desc())
        else:
            # 普通用户：所有者或成员
            member_subq = select(KbMember.kb_id).where(
                KbMember.user_id == self.user.id
            )
            stmt = (
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.deleted_at.is_(None),
                    or_(
                        KnowledgeBase.owner_id == self.user.id,
                        KnowledgeBase.id.in_(member_subq),
                    ),
                )
            )
            stmt = apply_tenant_filter(stmt, KnowledgeBase, self._tenant_id)
            stmt = stmt.order_by(KnowledgeBase.created_at.desc())

        return await paginate(stmt, params, self.db)

    # ------------------------------------------------------------------
    # 文档管理
    # ------------------------------------------------------------------

    async def upload_document(
        self,
        kb_id: UUID,
        title: str,
        content: str,
        doc_type: str = "md",
    ) -> Document:
        """向知识库上传 / 新建文档（校验知识库访问权限）。

        content 写入 content_text 字段（检索用纯文本），
        文档初始状态为 draft，owner 为当前用户。

        Args:
            kb_id: 目标知识库 ID。
            title: 文档标题。
            content: 文档纯文本内容。
            doc_type: 文档类型 — md / html / docx / pdf。

        Returns:
            创建后的 Document 实例。

        Raises:
            PermissionError: 当前用户无权向该知识库上传文档。
        """
        if not await self.permission.check_function(kb_id):
            raise PermissionError("无权向该知识库上传文档")

        return await self.doc_repo.create(
            kb_id=kb_id,
            title=title,
            content_text=content,
            doc_type=doc_type,
            owner_id=self.user.id,
            dept_id=self.user.dept_id,
            status="draft",
        )

    async def update_document(
        self,
        doc_id: UUID,
        content_html: str | None = None,
        content_json: dict | None = None,
        content_text: str | None = None,
    ) -> Document:
        """更新文档内容（校验知识库访问权限）。

        支持协同编辑场景：可同时传入 HTML、Tiptap JSON 与纯文本。
        仅传入非 None 的字段才会被更新。

        Args:
            doc_id: 文档 ID。
            content_html: HTML 内容（可选）。
            content_json: Tiptap JSON 结构（可选）。
            content_text: 纯文本内容，用于检索（可选）。

        Returns:
            更新后的 Document 实例。

        Raises:
            ValueError: 文档不存在。
            PermissionError: 当前用户无权编辑该文档所属知识库，或密级不足。
        """
        doc = await self.doc_repo.get_by_id(doc_id)
        if doc is None:
            raise ValueError(f"文档 {doc_id} 不存在")
        if not await self.permission.check_function(doc.kb_id):
            raise PermissionError("无权编辑该文档")
        # 密级校验（安全）：与 list_documents 保持一致 — 密级超过用户
        # clearance_level 的文档禁止修改，防止越权篡改。
        if doc.classification not in self.permission.allowed_classifications():
            raise PermissionError("密级不足，无权编辑该文档")

        # 仅更新非 None 字段，避免覆盖未传入的内容
        update_fields: dict = {}
        if content_html is not None:
            update_fields["content_html"] = content_html
        if content_json is not None:
            update_fields["content_json"] = content_json
        if content_text is not None:
            update_fields["content_text"] = content_text

        if update_fields:
            updated = await self.doc_repo.update(doc_id, **update_fields)
            if updated is None:
                raise ValueError(f"文档 {doc_id} 不存在")
            # P1: 文档更新后主动失效关联的 Token 缓存，避免返回过期答案
            await self._invalidate_cache_for_doc(str(doc_id))
            return updated
        return doc

    async def get_document(self, doc_id: UUID) -> Document:
        """获取单个文档详情（校验知识库访问权限 + 密级）。

        Args:
            doc_id: 文档 ID。

        Returns:
            Document 实例。

        Raises:
            ValueError: 文档不存在。
            PermissionError: 当前用户无权访问该文档所属知识库，或密级不足。
        """
        doc = await self.doc_repo.get_by_id(doc_id)
        if doc is None:
            raise ValueError(f"文档 {doc_id} 不存在")
        if not await self.permission.check_function(doc.kb_id):
            raise PermissionError("无权访问该文档")
        # 密级校验（安全）：与 list_documents 保持一致 — 密级超过用户
        # clearance_level 的文档禁止读取，防止越权访问。
        if doc.classification not in self.permission.allowed_classifications():
            raise PermissionError("密级不足，无权访问该文档")
        return doc

    async def list_documents(
        self, kb_id: UUID, page: int, size: int
    ) -> PageResult:
        """分页查询指定知识库下的文档列表。

        权限校验：
        1. 用户需可访问该知识库（check_function）；
        2. 文档密级不超过用户 clearance_level（SQL 层 IN 过滤，保证分页准确）。

        Args:
            kb_id: 知识库 ID。
            page: 页码（从 1 开始）。
            size: 每页条数（上限 100）。

        Returns:
            PageResult[Document]。

        Raises:
            PermissionError: 当前用户无权访问该知识库。
        """
        if not await self.permission.check_function(kb_id):
            raise PermissionError("无权访问该知识库")

        params = PaginationParams(page=page, size=size)
        allowed = self.permission.allowed_classifications()

        stmt = (
            select(Document)
            .where(
                Document.kb_id == kb_id,
                Document.deleted_at.is_(None),
                Document.classification.in_(allowed),
            )
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        stmt = stmt.order_by(Document.created_at.desc())
        return await paginate(stmt, params, self.db)

    # ------------------------------------------------------------------
    # P1: 缓存主动失效
    # ------------------------------------------------------------------

    async def _invalidate_cache_for_doc(self, doc_id: str) -> None:
        """文档更新/删除后失效关联的 Token 缓存。

        优雅降级：缓存服务不可用时仅记录日志，不影响文档操作。
        """
        try:
            from app.rag.cache import TokenCache

            cache = TokenCache()
            count = await cache.invalidate_by_doc_id(doc_id)
            if count > 0:
                from app.utils.logger import get_logger
                get_logger(__name__).info(
                    "knowledge.cache_invalidated",
                    doc_id=doc_id,
                    count=count,
                )
        except Exception:
            # 缓存失效失败不影响文档操作本身
            pass
