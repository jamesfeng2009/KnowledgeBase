"""
对话与消息仓储 — 单一职责：AI 问答会话领域的数据访问。

遵循单一职责：
- ConversationRepository 只处理对话表的查询；
- MessageRepository 只处理消息表的查询。

遵循开闭原则：
- 两者均继承 BaseRepository 获得标准 CRUD；
- 扩展添加各自领域的专属查询方法。

注意：Message 模型不支持软删除（无 SoftDeleteMixin），
因此 MessageRepository 的查询不包含 deleted_at 过滤条件。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.conversation import Conversation, Message
from app.repositories.base import BaseRepository


class ConversationRepository(BaseRepository[Conversation]):
    """对话仓储 — 封装对话表的领域查询。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Conversation, session)

    async def get_by_user(self, user_id: UUID) -> list[Conversation]:
        """查询某用户的所有对话（排除已软删除，按创建时间倒序）。"""
        stmt = (
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_with_messages(self, conversation_id: UUID) -> Conversation | None:
        """查询单条对话并预加载其所有消息（排除已软删除的对话）。

        使用 selectinload 策略避免 N+1 查询：
        - 第一条 SQL 查询对话；
        - 第二条 SQL 批量查询该对话下的所有消息。
        """
        stmt = (
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()


class MessageRepository(BaseRepository[Message]):
    """消息仓储 — 封装消息表的领域查询。

    Message 模型不支持软删除，所有查询不包含 deleted_at 过滤。
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Message, session)

    async def get_by_conversation(
        self, conv_id: UUID, limit: int | None = None
    ) -> list[Message]:
        """查询某对话下的所有消息（按创建时间正序，保证对话顺序）。

        Args:
            conv_id: 对话 ID。
            limit: 可选，仅返回最近 N 条消息（用于上下文窗口截断）。
                为 None 时返回全部消息。
        """
        stmt = (
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc())
        )
        if limit is not None and limit > 0:
            # 截断最近 N 条：子查询按倒序取 limit 条，外层再正序排列
            sub = (
                select(Message.id)
                .where(Message.conversation_id == conv_id)
                .order_by(Message.created_at.desc())
                .limit(limit)
            ).subquery()
            stmt = (
                select(Message)
                .where(Message.id.in_(select(sub.c.id)))
                .order_by(Message.created_at.asc())
            )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create_message(
        self,
        conv_id: UUID,
        role: str,
        content: str,
        **extra_fields,
    ) -> Message:
        """在指定对话下创建一条新消息。

        Args:
            conv_id: 对话 ID。
            role: 消息角色 — user/assistant/system。
            content: 消息文本内容。
            **extra_fields: 可选字段（如 sources、token_count、model_used）。
        """
        message = Message(
            conversation_id=conv_id,
            role=role,
            content=content,
            **extra_fields,
        )
        self.session.add(message)
        await self.session.flush()
        await self.session.refresh(message)
        return message
