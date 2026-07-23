"""
专家发现服务测试 — 测试专家查找和专业领域分析。

不依赖外部服务，使用 PostgreSQL 数据库。
"""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.comment import DocumentComment
from app.models.knowledge import Document, KnowledgeBase
from app.models.qa import QaAnswer, QaQuestion
from app.models.user import User
from app.services.expert_service import ExpertService


@pytest_asyncio.fixture
async def db_session():
    """创建 PostgreSQL 数据库用于测试。"""
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        # 先 drop 再 create — 清理前次测试残留数据，保证隔离
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
async def sample_data(db_session):
    """创建测试数据：用户、知识库、文档、问答、评论。"""
    # 创建用户
    user1 = User(
        email="expert@test.com",
        hashed_password="fake_hash",
        name="专家用户",
        role="editor",
    )
    user2 = User(
        email="commenter@test.com",
        hashed_password="fake_hash",
        name="评论用户",
        role="viewer",
    )
    db_session.add_all([user1, user2])
    await db_session.flush()

    # 创建知识库 — owner_id 必须指向已存在的 User，否则触发外键约束
    kb = KnowledgeBase(name="测试KB", owner_id=user1.id)
    db_session.add(kb)
    await db_session.flush()

    # 创建文档（user1 写了关于"微服务"的文档）
    doc1 = Document(
        kb_id=kb.id,
        title="微服务架构设计指南",
        content_html="<p>微服务架构详解</p>",
        doc_type="md",
        owner_id=user1.id,
        category="技术文档",
    )
    doc2 = Document(
        kb_id=kb.id,
        title="微服务部署最佳实践",
        content_html="<p>Docker 部署</p>",
        doc_type="md",
        owner_id=user1.id,
        category="技术文档",
    )
    doc3 = Document(
        kb_id=kb.id,
        title="项目管理流程",
        content_html="<p>项目流程</p>",
        doc_type="md",
        owner_id=user2.id,
        category="SOP",
    )
    db_session.add_all([doc1, doc2, doc3])
    await db_session.flush()

    # 创建问答（user1 回答了微服务相关问题）
    question = QaQuestion(
        user_id=user2.id,
        title="微服务如何拆分？",
        content="请问微服务架构怎么拆分？",
    )
    db_session.add(question)
    await db_session.flush()

    answer = QaAnswer(
        question_id=question.id,
        user_id=user1.id,
        content="微服务拆分可以按业务域划分...",
        is_accepted=True,
    )
    db_session.add(answer)

    # user2 评论了 user1 的文档
    comment = DocumentComment(
        doc_id=doc1.id,
        user_id=user2.id,
        content="这篇微服务文章写得很好",
    )
    db_session.add(comment)
    await db_session.flush()

    return {"user1": user1, "user2": user2, "doc1": doc1}


@pytest.mark.asyncio
class TestExpertService:
    """专家发现服务测试。"""

    async def test_find_experts_by_keyword(self, db_session, sample_data):
        """按关键词查找专家 — 应找到写了微服务文档的用户。"""
        service = ExpertService(db_session)
        experts = await service.find_experts(keyword="微服务", top_k=5)

        assert len(experts) > 0
        # user1 写了 2 篇微服务文档 + 1 个采纳回答，分数应最高
        top_expert = experts[0]
        assert top_expert["name"] == "专家用户"
        assert top_expert["score"] > 0

    async def test_find_experts_empty_keyword(self, db_session):
        """空关键词应返回空列表。"""
        service = ExpertService(db_session)
        experts = await service.find_experts(keyword="", top_k=5)
        assert experts == []

    async def test_find_experts_no_match(self, db_session, sample_data):
        """不匹配的关键词应返回空列表。"""
        service = ExpertService(db_session)
        experts = await service.find_experts(keyword="量子计算", top_k=5)
        assert experts == []

    async def test_get_user_expertise(self, db_session, sample_data):
        """获取用户专业领域 — 应返回文档分类统计。"""
        service = ExpertService(db_session)
        expertise = await service.get_user_expertise(str(sample_data["user1"].id))

        assert len(expertise) > 0
        # user1 的文档都是"技术文档"分类
        tech = [e for e in expertise if e["keyword"] == "技术文档"]
        assert len(tech) == 1
        assert tech[0]["doc_count"] == 2

    async def test_get_user_expertise_invalid_uuid(self, db_session):
        """无效 UUID 应返回空列表。"""
        service = ExpertService(db_session)
        expertise = await service.get_user_expertise("invalid-uuid")
        assert expertise == []

    async def test_find_experts_score_ordering(self, db_session, sample_data):
        """专家分数应按降序排列。"""
        service = ExpertService(db_session)
        experts = await service.find_experts(keyword="微服务", top_k=10)

        if len(experts) >= 2:
            assert experts[0]["score"] >= experts[1]["score"]

    # ------------------------------------------------------------------
    # P0 解耦：get_top_contributors 测试
    # ------------------------------------------------------------------

    async def test_get_top_contributors(self, db_session, sample_data):
        """贡献排行 — user1 有 2 文档 + 1 采纳回答，user2 有 1 文档 + 1 评论。"""
        service = ExpertService(db_session)
        contributors = await service.get_top_contributors(days=30, top_k=10)

        assert len(contributors) >= 2
        # user1 分数应高于 user2（2 文档 + 1 采纳回答 vs 1 文档 + 1 评论）
        assert contributors[0]["score"] >= contributors[1]["score"]
        # 验证返回字段包含 name
        for c in contributors:
            assert "user_id" in c
            assert "name" in c
            assert "score" in c

    async def test_get_top_contributors_with_names(self, db_session, sample_data):
        """贡献排行应包含用户姓名。"""
        service = ExpertService(db_session)
        contributors = await service.get_top_contributors(days=30, top_k=10)
        names = {c["name"] for c in contributors}
        assert "专家用户" in names
        assert "评论用户" in names

    async def test_get_top_contributors_top_k_limit(self, db_session, sample_data):
        """top_k 参数应限制返回数量。"""
        service = ExpertService(db_session)
        contributors = await service.get_top_contributors(days=30, top_k=1)
        assert len(contributors) == 1

    async def test_get_top_contributors_empty(self, db_session):
        """无数据时应返回空列表。"""
        service = ExpertService(db_session)
        contributors = await service.get_top_contributors(days=30, top_k=10)
        assert contributors == []

    async def test_get_top_contributors_score_weights(self, db_session, sample_data):
        """验证权重：文档 0.4 + 回答 0.2 + 采纳 0.1 + 评论 0.3。"""
        service = ExpertService(db_session)
        contributors = await service.get_top_contributors(days=365, top_k=10)

        # 找到 user1 (专家用户)
        user1_contrib = next(
            c for c in contributors if c["name"] == "专家用户"
        )
        # user1: 2 文档 * 0.4 + 1 回答 * 0.2 + 1 采纳 * 0.1 = 0.8 + 0.2 + 0.1 = 1.1
        assert user1_contrib["score"] == round(2 * 0.4 + 1 * 0.2 + 1 * 0.1, 2)

        # 找到 user2 (评论用户)
        user2_contrib = next(
            c for c in contributors if c["name"] == "评论用户"
        )
        # user2: 1 文档 * 0.4 + 1 评论 * 0.3 = 0.4 + 0.3 = 0.7
        assert user2_contrib["score"] == round(1 * 0.4 + 1 * 0.3, 2)
