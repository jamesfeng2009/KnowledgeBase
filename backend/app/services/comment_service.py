"""
文档评论服务 — 单一职责：编排文档异步讨论的业务流程。

遵循单一职责：CommentService 只负责评论领域的业务编排（发评论 / 查评论 / 解决评论），
不涉及文档内容编辑或审核流程，也不直接编写 SQL（委托 Repository）。

遵循开闭原则：通过依赖注入组合 DocumentCommentRepository，
新增评论能力只需追加方法，不修改既有方法实现。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import DocumentComment
from app.models.user import User
from app.repositories.comment_repository import DocumentCommentRepository
from app.repositories.knowledge_repository import DocumentRepository
from app.services.permission_service import PermissionService


class CommentService:
    """文档评论服务 — 封装文档异步讨论的业务编排。

    异常策略：资源不存在抛 ValueError，权限不足抛 PermissionError，
    由上层 API 层统一翻译为 HTTP 状态码。
    """

    def __init__(
        self, db: AsyncSession, user: User, tenant_id: UUID | None = None
    ) -> None:
        """初始化评论服务，注入依赖。

        Args:
            db: 异步数据库会话。
            user: 当前已认证用户。
            tenant_id: 租户 ID，用于多租户数据隔离。
        """
        self.db: AsyncSession = db
        self.user: User = user
        self._tenant_id = tenant_id
        self.comment_repo: DocumentCommentRepository = DocumentCommentRepository(
            db, tenant_id=tenant_id
        )
        self.doc_repo: DocumentRepository = DocumentRepository(
            db, tenant_id=tenant_id
        )
        self.permission: PermissionService = PermissionService(
            db, user, tenant_id=tenant_id
        )

    async def _check_doc_access(self, doc_id: UUID) -> None:
        """校验当前用户对文档所属知识库的访问权限（与文档读取对齐）。

        评论承载文档的讨论内容 —— 无校验时，同租户用户猜出机密知识库
        文档的 UUID 即可读取敏感讨论或发表垃圾评论（越权读写）。
        """
        doc = await self.doc_repo.get_by_id(doc_id)
        if doc is None:
            raise ValueError(f"文档 {doc_id} 不存在")
        if not await self.permission.check_function(doc.kb_id):
            raise PermissionError("无权访问该文档")
        if doc.classification not in self.permission.allowed_classifications():
            raise PermissionError("密级不足，无权访问该文档")

    # ------------------------------------------------------------------
    # 评论操作
    # ------------------------------------------------------------------

    async def create_comment(
        self,
        doc_id: UUID,
        content: str,
        parent_id: UUID | None = None,
    ) -> DocumentComment:
        """在文档下发表评论或回复。

        支持 thread 讨论：parent_id 指定时为对某条评论的回复，
        为 None 时为文档的顶层评论。

        Args:
            doc_id: 文档 ID。
            content: 评论内容。
            parent_id: 父评论 ID（可选，用于回复）。

        Returns:
            创建后的 DocumentComment 实例。

        Raises:
            ValueError: 文档不存在。
            PermissionError: 无权访问该文档所属知识库或密级不足。
        """
        await self._check_doc_access(doc_id)
        return await self.comment_repo.create(
            doc_id=doc_id,
            user_id=self.user.id,
            content=content,
            parent_id=parent_id,
        )

    async def list_comments(self, doc_id: UUID) -> list[DocumentComment]:
        """查询指定文档下的顶层评论列表。

        仅返回 parent_id IS NULL 的直接评论（按时间正序）。
        子回复通过单独接口懒加载，避免一次加载过深嵌套。

        Args:
            doc_id: 文档 ID。

        Returns:
            顶层 DocumentComment 列表。

        Raises:
            ValueError: 文档不存在。
            PermissionError: 无权访问该文档所属知识库或密级不足。
        """
        await self._check_doc_access(doc_id)
        return await self.comment_repo.get_by_doc(doc_id)

    async def resolve_comment(self, comment_id: UUID) -> DocumentComment:
        """标记评论为已解决（仅评论作者或 admin 可操作）。

        用于协同场景下关闭讨论线程，表示该评论关注的问题已处理。

        Args:
            comment_id: 评论 ID。

        Returns:
            更新后的 DocumentComment 实例。

        Raises:
            ValueError: 评论不存在。
            PermissionError: 当前用户无权解决该评论。
        """
        comment = await self.comment_repo.get_by_id(comment_id)
        if comment is None:
            raise ValueError(f"评论 {comment_id} 不存在")

        if comment.user_id != self.user.id and self.user.role != "admin":
            raise PermissionError("仅评论作者或管理员可解决评论")

        updated = await self.comment_repo.update(comment_id, resolved=True)
        if updated is None:
            raise ValueError(f"评论 {comment_id} 不存在")
        return updated
