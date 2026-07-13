"""
文档评论仓储 — 单一职责：文档评论领域的数据访问。

遵循单一职责：DocumentCommentRepository 只处理评论表的查询，
不涉及文档内容编辑或审核流程。

遵循开闭原则：继承 BaseRepository 获得标准 CRUD，
扩展添加评论专属查询（按文档、按父评论）。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import DocumentComment
from app.repositories.base import BaseRepository


class DocumentCommentRepository(BaseRepository[DocumentComment]):
    """文档评论仓储 — 封装评论表的领域查询。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(DocumentComment, session)

    async def get_by_doc(self, doc_id: UUID) -> list[DocumentComment]:
        """查询指定文档下的顶层评论（排除已软删除）。

        只返回 parent_id IS NULL 的评论（即直接评论，非回复）。
        子回复通过 get_replies(comment_id) 单独获取，支持懒加载。
        """
        stmt = (
            select(DocumentComment)
            .where(
                DocumentComment.doc_id == doc_id,
                DocumentComment.parent_id.is_(None),
                DocumentComment.deleted_at.is_(None),
            )
            .order_by(DocumentComment.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_replies(self, comment_id: UUID) -> list[DocumentComment]:
        """查询指定评论的直接回复（排除已软删除）。

        返回 parent_id = comment_id 的评论列表，按时间正序排列。
        若需获取多层嵌套回复，调用方可递归调用此方法。
        """
        stmt = (
            select(DocumentComment)
            .where(
                DocumentComment.parent_id == comment_id,
                DocumentComment.deleted_at.is_(None),
            )
            .order_by(DocumentComment.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
