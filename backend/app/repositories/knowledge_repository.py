"""
知识库与文档仓储 — 单一职责：知识库和文档领域的数据访问。

遵循单一职责：
- KnowledgeBaseRepository 只处理知识库表的查询；
- DocumentRepository 只处理文档表的查询。

遵循开闭原则：
- 两者均继承 BaseRepository 获得标准 CRUD；
- 扩展添加各自领域的专属查询方法。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Document, KnowledgeBase
from app.models.user import KbMember
from app.repositories.base import BaseRepository


class KnowledgeBaseRepository(BaseRepository[KnowledgeBase]):
    """知识库仓储 — 封装知识库表的领域查询。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(KnowledgeBase, session)

    async def get_by_owner(self, owner_id: UUID) -> list[KnowledgeBase]:
        """查询某用户拥有的所有知识库（排除已软删除）。"""
        stmt = (
            select(KnowledgeBase)
            .where(
                KnowledgeBase.owner_id == owner_id,
                KnowledgeBase.deleted_at.is_(None),
            )
            .order_by(KnowledgeBase.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_accessible_kbs(self, user_id: UUID) -> list[KnowledgeBase]:
        """查询用户可访问的所有知识库（排除已软删除）。

        可访问条件（OR 逻辑）：
        1. 用户是知识库的所有者（owner_id）；
        2. 用户是知识库的成员（通过 kb_members 关联表）。
        """
        member_subq = select(KbMember.kb_id).where(KbMember.user_id == user_id)
        stmt = (
            select(KnowledgeBase)
            .where(
                KnowledgeBase.deleted_at.is_(None),
                or_(
                    KnowledgeBase.owner_id == user_id,
                    KnowledgeBase.id.in_(member_subq),
                ),
            )
            .order_by(KnowledgeBase.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class DocumentRepository(BaseRepository[Document]):
    """文档仓储 — 封装文档表的领域查询。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Document, session)

    async def get_by_kb(self, kb_id: UUID) -> list[Document]:
        """查询指定知识库下的所有文档（排除已软删除）。"""
        stmt = (
            select(Document)
            .where(
                Document.kb_id == kb_id,
                Document.deleted_at.is_(None),
            )
            .order_by(Document.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_classification(self, classification: str) -> list[Document]:
        """按密级查询文档（排除已软删除）。

        Args:
            classification: 密级标识 — public/internal/confidential/secret。
        """
        stmt = (
            select(Document)
            .where(
                Document.classification == classification,
                Document.deleted_at.is_(None),
            )
            .order_by(Document.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_text(self, keyword: str) -> list[Document]:
        """全文模糊检索文档（排除已软删除）。

        在 title 和 content_text 两个字段上执行 ILIKE 不区分大小写匹配。
        content_text 字段在模型层标注为"检索用"纯文本。

        Args:
            keyword: 搜索关键词。
        """
        pattern = f"%{keyword}%"
        stmt = (
            select(Document)
            .where(
                Document.deleted_at.is_(None),
                or_(
                    Document.title.ilike(pattern),
                    Document.content_text.ilike(pattern),
                ),
            )
            .order_by(Document.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
