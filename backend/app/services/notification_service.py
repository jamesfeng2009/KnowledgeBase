"""
知识主动推送服务 — 单一职责：从被动响应变为主动推荐。

三种推送策略：
    1. 个性化日报（每日 9:00）— 基于用户部门+角色+浏览历史推荐 3 条知识
    2. 文档变更通知（事件驱动）— 通知关联文档关注者
    3. 知识缺口预警（每日 18:00）— 通知管理员高频无结果查询

推送渠道：站内通知（WebSocket）、邮件、IM Bot。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import DocumentComment
from app.models.knowledge import Document
from app.models.notification import Notification
from app.models.user import User
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

logger = get_logger(__name__)

#: 个性化日报推荐数量
DIGEST_COUNT: int = 3

#: 文档变更通知关联深度
RELATED_DEPTH: int = 1


class NotificationService:
    """知识主动推送服务 — 个性化推荐 + 变更通知 + 缺口预警。"""

    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None) -> None:
        self.db = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 1. 个性化知识日报
    # ------------------------------------------------------------------

    async def generate_personal_digest(
        self,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """生成个性化知识日报。

        推荐逻辑：
        1. 获取用户最近浏览/评论的文档 → 提取关键词
        2. 搜索包含这些关键词的其他文档
        3. 过滤已读 + 权限过滤
        4. 取 Top 3 推送

        Args:
            user_id: 用户 ID 字符串。

        Returns:
            推荐文档列表。
        """
        try:
            user_uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return []

        # 1. 获取用户最近活跃的文档（评论过的文档）
        recent_docs = await self._get_recent_activity_docs(user_uid, limit=5)
        if not recent_docs:
            # 无活跃记录 → 按热度推荐
            recent_docs = await self._get_popular_docs(limit=5)

        # 2. 提取关键词 → 搜索关联文档
        recommendations: list[dict[str, Any]] = []
        seen_ids: set[str] = set(str(d.id) for d in recent_docs)

        for doc in recent_docs:
            if len(recommendations) >= DIGEST_COUNT:
                break
            # 用文档标题关键词搜索关联文档
            related = await self._find_related_by_title(doc.title, exclude_ids=seen_ids)
            for r in related:
                if len(recommendations) < DIGEST_COUNT:
                    recommendations.append(r)
                    seen_ids.add(r["id"])

        # 3. 创建通知记录
        if recommendations:
            await self._create_notification(
                user_id=user_uid,
                notification_type="personal_digest",
                title="您的知识日报",
                content=self._format_digest(recommendations),
            )

        logger.info(
            "notification.digest_generated",
            user_id=user_id,
            count=len(recommendations),
        )
        return recommendations

    # ------------------------------------------------------------------
    # 2. 文档变更通知
    # ------------------------------------------------------------------

    async def notify_document_change(
        self,
        doc_id: str,
        change_type: str = "updated",
    ) -> int:
        """文档变更通知 — 通知关联文档关注者。

        通过评论和问答关系找到关注该文档的用户。

        Args:
            doc_id: 变更的文档 ID。
            change_type: 变更类型（updated/published/deprecated）。

        Returns:
            通知的用户数量。
        """
        try:
            doc_uid = uuid.UUID(doc_id)
        except (ValueError, TypeError):
            return 0

        # 找到评论过该文档的用户
        comment_stmt = (
            select(DocumentComment.user_id)
            .where(
                and_(
                    DocumentComment.doc_id == doc_uid,
                    DocumentComment.deleted_at.is_(None),
                )
            )
        )
        comment_stmt = apply_tenant_filter(comment_stmt, DocumentComment, self._tenant_id)
        comment_stmt = comment_stmt.distinct()
        comment_result = await self.db.execute(comment_stmt)
        user_ids = {r.user_id for r in comment_result if r.user_id}

        # 找到文档作者
        doc_stmt = select(Document.owner_id).where(Document.id == doc_uid)
        doc_stmt = apply_tenant_filter(doc_stmt, Document, self._tenant_id)
        doc_result = await self.db.execute(doc_stmt)
        doc_row = doc_result.first()
        if doc_row and doc_row.owner_id:
            user_ids.discard(doc_row.owner_id)  # 不通知作者自己

        if not user_ids:
            logger.info("notification.doc_change_no_users", doc_id=doc_id)
            return 0

        # 创建通知
        change_label = {
            "updated": "已更新",
            "published": "已发布",
            "deprecated": "已废弃",
        }.get(change_type, "有更新")

        for uid in user_ids:
            await self._create_notification(
                user_id=uid,
                notification_type="document_change",
                title="您关注的文档有更新",
                content=f"文档{change_label}，点击查看最新版本",
                doc_id=doc_uid,
            )

        logger.info(
            "notification.doc_change_sent",
            doc_id=doc_id,
            change_type=change_type,
            notified=len(user_ids),
        )
        return len(user_ids)

    # ------------------------------------------------------------------
    # 3. 知识缺口预警
    # ------------------------------------------------------------------

    async def send_gap_alert(self) -> int:
        """知识缺口预警 — 通知知识管理员。

        基于 gap_detector_service 已有的缺口检测逻辑。

        Returns:
            通知的管理员数量。
        """
        from app.services.gap_detector_service import GapDetectorService

        gap_service = GapDetectorService(self.db)
        gaps = await gap_service.detect_gaps()

        if not gaps:
            logger.info("notification.gap_alert_no_gaps")
            return 0

        # 通知所有 kb_admin 角色用户
        admins = await self._get_users_by_role("kb_admin")
        if not admins:
            return 0

        gap_topics = [g["topic"] for g in gaps[:5]]
        for admin in admins:
            await self._create_notification(
                user_id=admin.id,
                notification_type="gap_alert",
                title=f"发现 {len(gaps)} 个知识缺口",
                content=f"高频无结果查询：{', '.join(gap_topics)}",
            )

        logger.info(
            "notification.gap_alert_sent",
            gaps=len(gaps),
            admins=len(admins),
        )
        return len(admins)

    # ------------------------------------------------------------------
    # 通知管理
    # ------------------------------------------------------------------

    async def get_user_notifications(
        self,
        user_id: str,
        unread_only: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """获取用户的通知列表。

        Args:
            user_id: 用户 ID 字符串。
            unread_only: 是否只返回未读通知。
            limit: 返回数量上限。

        Returns:
            通知列表。
        """
        try:
            user_uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return []

        query = select(Notification).where(Notification.user_id == user_uid)
        if unread_only:
            query = query.where(Notification.is_read.is_(False))
        query = apply_tenant_filter(query, Notification, self._tenant_id)
        query = query.order_by(desc(Notification.created_at)).limit(limit)

        result = await self.db.execute(query)
        return [
            {
                "id": str(n.id),
                "type": n.notification_type,
                "title": n.title,
                "content": n.content,
                "doc_id": str(n.doc_id) if n.doc_id else None,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in result.scalars().all()
        ]

    async def count_unread(self, user_id: str) -> int:
        """统计用户未读通知数量（SQL COUNT，不受列表 limit 截断影响）。

        Args:
            user_id: 用户 ID 字符串。

        Returns:
            未读通知总数。
        """
        try:
            user_uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return 0

        stmt = select(func.count(Notification.id)).where(
            and_(
                Notification.user_id == user_uid,
                Notification.is_read.is_(False),
            )
        )
        stmt = apply_tenant_filter(stmt, Notification, self._tenant_id)
        result = await self.db.execute(stmt)
        return int(result.scalar() or 0)

    async def mark_as_read(self, notification_id: str, user_id: str) -> bool:
        """标记通知为已读。

        必须按当前用户过滤 —— 仅按通知 ID + 租户过滤时，同租户用户
        可持他人通知 ID 越权标记（IDOR），造成他人通知静默丢失。

        Args:
            notification_id: 通知 ID 字符串。
            user_id: 当前用户 ID 字符串（所有权校验）。

        Returns:
            是否成功。
        """
        try:
            notif_uid = uuid.UUID(notification_id)
            user_uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return False

        stmt = select(Notification).where(
            and_(
                Notification.id == notif_uid,
                Notification.user_id == user_uid,
            )
        )
        stmt = apply_tenant_filter(stmt, Notification, self._tenant_id)
        result = await self.db.execute(stmt)
        notification = result.scalars().first()
        if not notification:
            return False

        notification.is_read = True
        notification.read_at = datetime.utcnow()
        await self.db.flush()
        return True

    async def mark_all_read(self, user_id: str) -> int:
        """标记用户所有通知为已读。

        Args:
            user_id: 用户 ID 字符串。

        Returns:
            更新的通知数量。
        """
        try:
            user_uid = uuid.UUID(user_id)
        except (ValueError, TypeError):
            return 0

        stmt = select(Notification).where(
            and_(
                Notification.user_id == user_uid,
                Notification.is_read.is_(False),
            )
        )
        stmt = apply_tenant_filter(stmt, Notification, self._tenant_id)
        result = await self.db.execute(stmt)
        notifications = result.scalars().all()
        now = datetime.utcnow()
        for n in notifications:
            n.is_read = True
            n.read_at = now
        await self.db.flush()
        return len(notifications)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _get_recent_activity_docs(
        self, user_uid: uuid.UUID, limit: int = 5
    ) -> list[Document]:
        """获取用户最近评论过的文档。"""
        stmt = (
            select(Document)
            .join(DocumentComment, DocumentComment.doc_id == Document.id)
            .where(
                and_(
                    DocumentComment.user_id == user_uid,
                    DocumentComment.deleted_at.is_(None),
                    Document.deleted_at.is_(None),
                )
            )
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        stmt = stmt.order_by(desc(DocumentComment.created_at)).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _get_popular_docs(self, limit: int = 5) -> list[Document]:
        """按浏览量获取热门文档（用户无活跃记录时的兜底）。"""
        stmt = select(Document).where(Document.deleted_at.is_(None))
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        stmt = stmt.order_by(desc(Document.view_count)).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _find_related_by_title(
        self, title: str, exclude_ids: set[str]
    ) -> list[dict[str, Any]]:
        """通过标题关键词搜索关联文档。"""
        if not title:
            return []
        # 取标题前 2 个字符作为关键词（中文短词匹配）
        keyword = f"%{title[:2]}%"
        stmt = (
            select(Document)
            .where(
                and_(
                    Document.deleted_at.is_(None),
                    Document.title.ilike(keyword),
                )
            )
        )
        stmt = apply_tenant_filter(stmt, Document, self._tenant_id)
        stmt = stmt.order_by(desc(Document.view_count)).limit(DIGEST_COUNT)
        result = await self.db.execute(stmt)
        docs = []
        for doc in result.scalars().all():
            doc_id_str = str(doc.id)
            if doc_id_str not in exclude_ids:
                docs.append({
                    "id": doc_id_str,
                    "title": doc.title,
                    "summary": doc.summary or "",
                    "category": doc.category or "",
                })
        return docs

    async def _get_users_by_role(self, role: str) -> list[User]:
        """获取指定角色的所有活跃用户。"""
        stmt = select(User).where(
            and_(
                User.role == role,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        stmt = apply_tenant_filter(stmt, User, self._tenant_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _create_notification(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        title: str,
        content: str,
        doc_id: uuid.UUID | None = None,
    ) -> None:
        """创建通知记录并实时推送到用户。

        写入 PostgreSQL 后通过 Redis Pub/Sub 推送 SSE 事件。
        Redis 不可用时静默降级（通知仍写入 DB，只是不实时推送）。
        """
        notification = Notification(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            content=content,
            doc_id=doc_id,
            is_read=False,
            tenant_id=self._tenant_id,  # RLS WITH CHECK 要求写入行携带当前租户 ID
        )
        self.db.add(notification)
        await self.db.flush()

        # 实时推送（Redis Pub/Sub → SSE）
        from app.services.notification_hub import publish

        await publish(
            user_id,
            {
                "type": "notification",
                "notification_type": notification_type,
                "title": title,
                "content": content,
                "doc_id": str(doc_id) if doc_id else None,
                "notification_id": str(notification.id),
            },
        )

    @staticmethod
    def _format_digest(recommendations: list[dict[str, Any]]) -> str:
        """格式化日报内容。"""
        if not recommendations:
            return "今日暂无推荐"
        lines = ["为您推荐以下知识："]
        for i, rec in enumerate(recommendations, 1):
            lines.append(f"{i}. {rec.get('title', '未知文档')}")
            if rec.get("summary"):
                lines.append(f"   摘要：{rec['summary'][:50]}...")
        return "\n".join(lines)
