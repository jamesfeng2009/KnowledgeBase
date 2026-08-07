"""
知识推荐服务测试 — 覆盖行为上报、三路召回分阶段启停、RRF 融合、权限过滤。

遵循约定：不依赖外部服务（embedder / vector_store / graph_service 默认注入
为 None，触发优雅降级或不注入），仅用 PostgreSQL + 内存数据。
"""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.behavior import UserBehavior
from app.models.knowledge import Document, KnowledgeBase
from app.models.user import User
from app.services.recommendation_service import RecommendationService


@pytest_asyncio.fixture
async def db_session():
    """创建 PostgreSQL 数据库用于测试。"""
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = async_session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def dataset(db_session):
    """创建基础数据：2 用户 + 1 知识库 + 5 篇发布文档。"""
    u1 = User(email="u1@test.com", hashed_password="h", name="目标用户", role="editor")
    u2 = User(email="u2@test.com", hashed_password="h", name="相似用户", role="editor")
    db_session.add_all([u1, u2])
    await db_session.flush()

    kb = KnowledgeBase(name="推荐测试KB", owner_id=u1.id)
    db_session.add(kb)
    await db_session.flush()

    docs = []
    for i, title in enumerate(["文档A", "文档B", "文档C", "文档D", "文档E"], start=1):
        d = Document(
            kb_id=kb.id,
            title=title,
            content_html=f"<p>{title}内容</p>",
            content_text=f"{title}内容",
            doc_type="md",
            status="published",
            owner_id=u1.id,
            view_count=i * 10,
        )
        docs.append(d)
    db_session.add_all(docs)
    await db_session.flush()
    return {"u1": u1, "u2": u2, "kb": kb, "docs": docs}


async def _add_behavior(db, user_id, doc_id, action="view", n=1):
    """为指定用户录入 n 次行为（每次累加权重）。"""
    svc = RecommendationService(db)
    for _ in range(n):
        await svc.record_behavior(user_id, doc_id, action)


class TestRecordBehavior:
    """行为上报测试。"""

    async def test_record_creates_row(self, db_session, dataset):
        svc = RecommendationService(db_session)
        await svc.record_behavior(dataset["u1"].id, dataset["docs"][0].id, "view")
        await db_session.commit()

        rows = (await db_session.execute(
            select(UserBehavior)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].weight == 1.0
        assert rows[0].action_type == "view"

    async def test_record_upserts_weight(self, db_session, dataset):
        svc = RecommendationService(db_session)
        await _add_behavior(db_session, dataset["u1"].id, dataset["docs"][0].id, "view", n=3)
        await db_session.commit()

        rows = (await db_session.execute(
            select(UserBehavior)
        )).scalars().all()
        # 同 (user, doc, action) 应合并为一行，权重累加
        assert len(rows) == 1
        assert rows[0].weight == 3.0

    async def test_record_action_type_weight(self, db_session, dataset):
        svc = RecommendationService(db_session)
        await svc.record_behavior(dataset["u1"].id, dataset["docs"][0].id, "like")
        await db_session.commit()
        rows = (await db_session.execute(
            select(UserBehavior)
        )).scalars().all()
        assert rows[0].weight == 3.0  # like 权重 3.0

    async def test_record_invalid_action_raises(self, db_session, dataset):
        svc = RecommendationService(db_session)
        with pytest.raises(ValueError):
            await svc.record_behavior(dataset["u1"].id, dataset["docs"][0].id, "hack")


class TestColdStart:
    """冷启动：无行为用户 → 热门兜底。"""

    async def test_no_behavior_returns_hot(self, db_session, dataset):
        svc = RecommendationService(db_session)
        result = await svc.recommend_for_user(dataset["u1"].id, top_k=5)
        assert len(result) > 0
        # 热门兜底的 reason 应为 hot
        assert all(r["reason"] == "hot" for r in result)
        # 返回了文档标题
        assert all(r["title"] for r in result)

    async def test_cf_disabled_below_threshold(self, db_session, dataset):
        """行为数 < 阈值时应关闭协同过滤（冷启动阶段）。"""
        # 只录 1 次行为（低于默认阈值 3）
        await _add_behavior(db_session, dataset["u1"].id, dataset["docs"][0].id, n=1)
        await db_session.commit()

        svc = RecommendationService(db_session)
        result = await svc.recommend_for_user(dataset["u1"].id, top_k=10)
        # 协同过滤未启用（无 embedder/图谱/CF），应回退热门且不含 cf
        assert result
        assert not any("cf" in r["reason"] for r in result)


class TestCollaborativeFiltering:
    """协同过滤：行为数达标后启用，UserCF/ItemCF 生效。"""

    async def test_cf_recommends_similar_user_doc(self, db_session, dataset):
        """u1 与 u2 共享 3 篇文档，u2 额外看过 d4 → 应推荐 d4。"""
        docs = dataset["docs"]
        # u1 看 d1,d2,d3
        for d in docs[:3]:
            await _add_behavior(db_session, dataset["u1"].id, d.id, n=1)
        # u2 看 d1,d2,d3,d4
        for d in docs[:4]:
            await _add_behavior(db_session, dataset["u2"].id, d.id, n=1)
        await db_session.commit()

        svc = RecommendationService(db_session)
        result = await svc.recommend_for_user(dataset["u1"].id, top_k=10)
        doc_ids = {r["doc_id"] for r in result}
        # d4 应被 UserCF/ItemCF 召回
        assert str(docs[3].id) in doc_ids
        # 不应包含 u1 已看过的 d1-d3
        assert not ({str(docs[i].id) for i in range(3)} & doc_ids)

    async def test_cf_off_for_isolated_user(self, db_session, dataset):
        """仅 u1 有行为、无相似用户 → 协同过滤无结果，回退热门。"""
        await _add_behavior(db_session, dataset["u1"].id, dataset["docs"][0].id, n=3)
        await db_session.commit()

        svc = RecommendationService(db_session)
        result = await svc.recommend_for_user(dataset["u1"].id, top_k=10)
        assert result
        assert not any("cf" in r["reason"] for r in result)


class TestRrfFuse:
    """RRF 融合纯函数测试。"""

    def test_rrf_fuses_rank_position(self):
        path1 = [
            {"doc_id": "a", "score": 1, "reason": "user_cf"},
            {"doc_id": "b", "score": 1, "reason": "user_cf"},
        ]
        path2 = [
            {"doc_id": "b", "score": 1, "reason": "vector"},
            {"doc_id": "c", "score": 1, "reason": "vector"},
        ]
        fused = RecommendationService._rrf_fuse([path1, path2], k=60)
        # b 在两路出现，融合分应最高
        assert fused[0]["doc_id"] == "b"
        assert "user_cf" in fused[0]["reason"]
        assert "vector" in fused[0]["reason"]

    def test_rrf_rank_score(self):
        path = [{"doc_id": "a", "score": 1, "reason": "x"}]
        fused = RecommendationService._rrf_fuse([path], k=60)
        assert fused[0]["score"] == round(1.0 / 61, 4)


class TestPermissionFilter:
    """权限过滤：仅返回可见文档。"""

    async def test_permission_filter_filters(self, db_session, dataset):
        docs = dataset["docs"]
        for d in docs[:3]:
            await _add_behavior(db_session, dataset["u1"].id, d.id, n=1)
        for d in docs[:4]:
            await _add_behavior(db_session, dataset["u2"].id, d.id, n=1)
        await db_session.commit()

        svc = RecommendationService(db_session)
        # 权限过滤：只允许 d4
        async def only_d4(doc_list):
            return [d for d in doc_list if d.id == docs[3].id]

        result = await svc.recommend_for_user(
            dataset["u1"].id, top_k=10, permission_filter=only_d4
        )
        assert result
        assert all(r["doc_id"] == str(docs[3].id) for r in result)


class TestRelatedDocuments:
    """相关阅读：复用 GraphService（此处 mock）。"""

    async def test_related_with_mocked_graph(self, db_session, dataset):
        docs = dataset["docs"]

        class FakeGraph:
            async def get_related_recommendations(self, doc_id, user_id, top_k=5,
                                                  permission_filter=None, db_session=None):
                return [
                    {"id": str(docs[1].id), "title": doc.title, "depth": 1,
                     "score": 1.0}
                    for doc in [docs[1]]
                ]

        svc = RecommendationService(db_session, graph_service=FakeGraph())
        result = await svc.get_related_documents(docs[0].id, dataset["u1"].id, top_k=5)
        assert len(result) == 1
        assert result[0]["doc_id"] == str(docs[1].id)
        assert result[0]["reason"] == "related"

    async def test_related_empty_when_graph_empty(self, db_session, dataset):
        class EmptyGraph:
            async def get_related_recommendations(self, doc_id, user_id, top_k=5,
                                                  permission_filter=None, db_session=None):
                return []

        svc = RecommendationService(db_session, graph_service=EmptyGraph())
        result = await svc.get_related_documents(dataset["docs"][0].id, dataset["u1"].id)
        assert result == []