"""
知识主动推送服务测试 — 测试个性化日报、文档变更通知、缺口预警。

不依赖外部服务，使用 SQLite 内存数据库。
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.comment import DocumentComment
from app.models.gap import KnowledgeGap
from app.models.knowledge import Document, KnowledgeBase
from app.models.notification import Notification
from app.models.user import User
from app.services.notification_service import NotificationService


@pytest_asyncio.fixture
async def db_session():
    """创建 SQLite 内存数据库用于测试。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = async_session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def sample_data(db_session):
    """创建测试数据：用户、知识库、文档、评论。"""
    owner = uuid.uuid4()

    user1 = User(
        email="user1@test.com",
        hashed_password="fake_hash",
        name="普通用户",
        role="viewer",
    )
    admin1 = User(
        email="admin@test.com",
        hashed_password="fake_hash",
        name="管理员",
        role="kb_admin",
    )
    db_session.add_all([user1, admin1])
    await db_session.flush()

    kb = KnowledgeBase(name="测试KB", owner_id=owner)
    db_session.add(kb)
    await db_session.flush()

    doc1 = Document(
        kb_id=kb.id,
        title="微服务架构指南",
        content_html="<p>微服务详解</p>",
        doc_type="md",
        owner_id=owner,
        view_count=100,
    )
    doc2 = Document(
        kb_id=kb.id,
        title="微服务部署最佳实践",
        content_html="<p>Docker 部署</p>",
        doc_type="md",
        owner_id=owner,
        view_count=50,
    )
    db_session.add_all([doc1, doc2])
    await db_session.flush()

    # user1 评论了 doc1
    comment = DocumentComment(
        doc_id=doc1.id,
        user_id=user1.id,
        content="很有帮助",
    )
    db_session.add(comment)
    await db_session.flush()

    return {
        "user1": user1,
        "admin1": admin1,
        "doc1": doc1,
        "doc2": doc2,
        "kb": kb,
    }


@pytest.mark.asyncio
class TestNotificationService:
    """知识主动推送服务测试。"""

    async def test_generate_personal_digest_with_activity(self, db_session, sample_data):
        """有评论记录的用户应生成个性化日报。"""
        service = NotificationService(db_session)
        recs = await service.generate_personal_digest(str(sample_data["user1"].id))

        # 用户评论了"微服务架构指南"，应推荐相关文档
        assert isinstance(recs, list)

        # 应创建通知记录
        result = await db_session.execute(select(Notification))
        notifications = result.scalars().all()
        assert len(notifications) >= 1
        assert notifications[0].notification_type == "personal_digest"
        assert notifications[0].user_id == sample_data["user1"].id

    async def test_generate_personal_digest_no_activity(self, db_session, sample_data):
        """无活跃记录的用户应按热度推荐。"""
        service = NotificationService(db_session)
        recs = await service.generate_personal_digest(str(sample_data["admin1"].id))

        # admin1 没有评论过任何文档，走热门文档兜底
        assert isinstance(recs, list)

    async def test_generate_personal_digest_invalid_uuid(self, db_session):
        """无效 UUID 应返回空列表。"""
        service = NotificationService(db_session)
        recs = await service.generate_personal_digest("invalid-uuid")
        assert recs == []

    async def test_notify_document_change(self, db_session, sample_data):
        """文档变更应通知评论过该文档的用户。"""
        service = NotificationService(db_session)
        notified = await service.notify_document_change(
            doc_id=str(sample_data["doc1"].id),
            change_type="updated",
        )

        # user1 评论了 doc1，应被通知
        assert notified == 1

        # 验证通知记录
        result = await db_session.execute(select(Notification))
        notifications = result.scalars().all()
        assert len(notifications) == 1
        assert notifications[0].notification_type == "document_change"
        assert "更新" in notifications[0].title

    async def test_notify_document_change_no_commenters(self, db_session, sample_data):
        """无评论者的文档不应产生通知。"""
        service = NotificationService(db_session)
        notified = await service.notify_document_change(
            doc_id=str(sample_data["doc2"].id),
            change_type="updated",
        )
        assert notified == 0

    async def test_notify_document_change_invalid_uuid(self, db_session):
        """无效文档 ID 应返回 0。"""
        service = NotificationService(db_session)
        notified = await service.notify_document_change(
            doc_id="invalid-uuid",
            change_type="updated",
        )
        assert notified == 0

    async def test_get_user_notifications(self, db_session, sample_data):
        """获取用户通知列表。"""
        service = NotificationService(db_session)

        # 先创建一条通知
        await service.notify_document_change(
            doc_id=str(sample_data["doc1"].id),
            change_type="updated",
        )
        await db_session.flush()

        # 获取通知列表
        notifications = await service.get_user_notifications(
            user_id=str(sample_data["user1"].id),
        )
        assert len(notifications) == 1
        assert notifications[0]["type"] == "document_change"

    async def test_get_unread_notifications(self, db_session, sample_data):
        """获取未读通知。"""
        service = NotificationService(db_session)
        await service.notify_document_change(
            doc_id=str(sample_data["doc1"].id),
            change_type="updated",
        )
        await db_session.flush()

        # 全部通知
        all_notifs = await service.get_user_notifications(
            user_id=str(sample_data["user1"].id),
            unread_only=False,
        )
        assert len(all_notifs) == 1

        # 未读通知
        unread = await service.get_user_notifications(
            user_id=str(sample_data["user1"].id),
            unread_only=True,
        )
        assert len(unread) == 1
        assert unread[0]["is_read"] is False

    async def test_mark_as_read(self, db_session, sample_data):
        """标记单条通知为已读。"""
        service = NotificationService(db_session)
        await service.notify_document_change(
            doc_id=str(sample_data["doc1"].id),
            change_type="updated",
        )
        await db_session.flush()

        # 获取通知 ID
        notifications = await service.get_user_notifications(
            user_id=str(sample_data["user1"].id),
        )
        notif_id = notifications[0]["id"]

        # 标记已读
        success = await service.mark_as_read(notif_id)
        assert success is True

        # 验证已读状态
        unread = await service.get_user_notifications(
            user_id=str(sample_data["user1"].id),
            unread_only=True,
        )
        assert len(unread) == 0

    async def test_mark_as_read_invalid_id(self, db_session):
        """无效通知 ID 应返回 False。"""
        service = NotificationService(db_session)
        success = await service.mark_as_read("invalid-uuid")
        assert success is False

    async def test_mark_all_read(self, db_session, sample_data):
        """标记所有通知为已读。"""
        service = NotificationService(db_session)

        # 创建多条通知
        await service.notify_document_change(
            doc_id=str(sample_data["doc1"].id),
            change_type="updated",
        )
        await db_session.flush()
        await service.notify_document_change(
            doc_id=str(sample_data["doc1"].id),
            change_type="published",
        )
        await db_session.flush()

        # 标记全部已读
        count = await service.mark_all_read(str(sample_data["user1"].id))
        assert count == 2

        # 验证无未读
        unread = await service.get_user_notifications(
            user_id=str(sample_data["user1"].id),
            unread_only=True,
        )
        assert len(unread) == 0

    async def test_send_gap_alert_with_gaps(self, db_session, sample_data):
        """有知识缺口时应通知管理员。"""
        # 创建高频缺口
        gap = KnowledgeGap(
            topic="报销流程",
            search_count=10,
            priority="high",
            status="open",
        )
        db_session.add(gap)
        await db_session.flush()

        service = NotificationService(db_session)
        notified = await service.send_gap_alert()

        # admin1 是 kb_admin
        assert notified == 1

        # 验证通知
        result = await db_session.execute(select(Notification))
        notifications = result.scalars().all()
        assert len(notifications) == 1
        assert notifications[0].notification_type == "gap_alert"
        assert "知识缺口" in notifications[0].title

    async def test_send_gap_alert_no_gaps(self, db_session, sample_data):
        """无知识缺口时不应通知。"""
        service = NotificationService(db_session)
        notified = await service.send_gap_alert()
        assert notified == 0
