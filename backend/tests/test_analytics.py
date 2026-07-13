"""
知识健康度分析测试 — 测试搜索日志记录和指标计算逻辑。

不依赖外部服务，使用 SQLite 内存数据库。
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.action import DocumentAction
from app.models.analytics import SearchLog
from app.models.base import Base
from app.models.knowledge import Document, KnowledgeBase
from app.models.user import User
from app.services.analytics_service import AnalyticsService


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


@pytest.mark.asyncio
class TestSearchLog:
    """搜索日志记录测试。"""

    async def test_log_search_creates_record(self, db_session):
        """记录搜索行为后，数据库中应有对应记录。"""
        service = AnalyticsService(db_session)
        await service.log_search(
            query="报销流程",
            user_id=None,
            source="knowledge_base",
            result_count=5,
        )
        await db_session.commit()

        result = await db_session.execute(select(SearchLog))
        logs = result.scalars().all()
        assert len(logs) == 1
        assert logs[0].query == "报销流程"
        assert logs[0].result_count == 5
        assert logs[0].clicked is False

    async def test_log_multiple_searches(self, db_session):
        """多次搜索记录。"""
        service = AnalyticsService(db_session)
        for i in range(5):
            await service.log_search(query=f"query_{i}", result_count=i)
        await db_session.commit()

        result = await db_session.execute(select(SearchLog))
        logs = result.scalars().all()
        assert len(logs) == 5


@pytest.mark.asyncio
class TestAnalyticsMetrics:
    """分析指标测试。"""

    async def test_search_hotwords(self, db_session):
        """搜索热词统计。"""
        service = AnalyticsService(db_session)
        # 创建 3 条 "报销" 和 2 条 "请假"
        for _ in range(3):
            await service.log_search(query="报销", result_count=1)
        for _ in range(2):
            await service.log_search(query="请假", result_count=1)
        await db_session.commit()

        hotwords = await service.get_search_hotwords(days=30, top_k=10)
        assert len(hotwords) == 2
        # "报销" 搜索次数更多，应排第一
        assert hotwords[0]["keyword"] == "报销"
        assert hotwords[0]["count"] == 3

    async def test_zero_click_queries(self, db_session):
        """零点击查询统计。"""
        service = AnalyticsService(db_session)
        # 创建不同数量的零点击查询
        for _ in range(3):
            await service.log_search(query="无结果查询", result_count=0)
        for _ in range(1):
            await service.log_search(query="有结果查询", result_count=5)
        await db_session.commit()

        zero_click = await service.get_zero_click_queries(days=30)
        assert len(zero_click) == 2  # 两条都是 clicked=False
        # "无结果查询" 搜索次数更多，应排第一
        assert zero_click[0]["keyword"] == "无结果查询"
        assert zero_click[0]["count"] == 3

    async def test_knowledge_coverage_empty(self, db_session):
        """空数据库的覆盖率应为 0。"""
        service = AnalyticsService(db_session)
        coverage = await service.get_knowledge_coverage()
        assert coverage["covered_topics"] == 0
        assert coverage["searched_topics"] == 0
        assert coverage["coverage_ratio"] == 0

    async def test_knowledge_coverage_with_data(self, db_session):
        """有数据时的覆盖率计算。"""
        # 创建知识库和文档（SQLite 不强制外键，用 uuid4 占位）
        owner = uuid.uuid4()
        kb = KnowledgeBase(name="测试KB", owner_id=owner)
        db_session.add(kb)
        await db_session.flush()

        doc1 = Document(
            kb_id=kb.id,
            title="测试文档1",
            content_html="<p>测试</p>",
            doc_type="md",
            category="技术文档",
            owner_id=owner,
        )
        doc2 = Document(
            kb_id=kb.id,
            title="测试文档2",
            content_html="<p>测试</p>",
            doc_type="md",
            category="会议纪要",
            owner_id=owner,
        )
        db_session.add_all([doc1, doc2])
        await db_session.flush()

        # 记录搜索
        service = AnalyticsService(db_session)
        await service.log_search(query="技术文档", result_count=1)
        await service.log_search(query="会议纪要", result_count=1)
        await service.log_search(query="不存在的主题", result_count=0)
        await db_session.commit()

        coverage = await service.get_knowledge_coverage()
        assert coverage["covered_topics"] == 2  # 2 种分类
        assert coverage["searched_topics"] == 3  # 3 种搜索词
        assert coverage["coverage_ratio"] > 0

    async def test_dashboard_returns_all_metrics(self, db_session):
        """仪表盘汇总应返回所有六项指标。"""
        service = AnalyticsService(db_session)
        dashboard = await service.get_dashboard(days=30)

        expected_keys = [
            "search_hotwords", "zero_click_queries", "popular_documents",
            "knowledge_coverage", "knowledge_freshness", "top_contributors",
        ]
        for key in expected_keys:
            assert key in dashboard, f"Missing metric: {key}"

    # ------------------------------------------------------------------
    # P1 解耦：knowledge_freshness PG 降级测试
    # ------------------------------------------------------------------

    async def test_freshness_from_pg_empty(self, db_session):
        """PG 降级 — 空数据库新鲜度应为 1.0（无过期文档）。"""
        service = AnalyticsService(db_session)
        result = await service._freshness_from_pg()

        assert result["total_documents"] == 0
        assert result["expired"] == 0
        assert result["expiring_soon"] == 0
        assert result["freshness_rate"] == 1.0
        assert result["source"] == "postgresql"

    async def test_freshness_from_pg_with_fresh_docs(self, db_session):
        """PG 降级 — 新文档不算过期。"""
        owner = uuid.uuid4()
        kb = KnowledgeBase(name="测试KB", owner_id=owner)
        db_session.add(kb)
        await db_session.flush()

        doc = Document(
            kb_id=kb.id,
            title="新文档",
            content_html="<p>新</p>",
            doc_type="md",
            category="技术",
            owner_id=owner,
        )
        db_session.add(doc)
        await db_session.flush()

        service = AnalyticsService(db_session)
        result = await service._freshness_from_pg()

        assert result["total_documents"] == 1
        assert result["expired"] == 0
        assert result["freshness_rate"] == 1.0
        assert result["source"] == "postgresql"

    async def test_freshness_from_pg_with_expired_doc(self, db_session):
        """PG 降级 — 超过过期阈值的文档算过期。"""
        from datetime import datetime, timedelta

        from app.services.analytics_service import FRESHNESS_EXPIRE_DAYS

        owner = uuid.uuid4()
        kb = KnowledgeBase(name="测试KB", owner_id=owner)
        db_session.add(kb)
        await db_session.flush()

        # 创建一个"过期"文档（updated_at 设为很久以前）
        old_date = datetime.utcnow() - timedelta(days=FRESHNESS_EXPIRE_DAYS + 10)
        doc = Document(
            kb_id=kb.id,
            title="过期文档",
            content_html="<p>旧</p>",
            doc_type="md",
            category="技术",
            owner_id=owner,
        )
        db_session.add(doc)
        await db_session.flush()
        # 手动修改 updated_at
        doc.updated_at = old_date
        await db_session.flush()

        service = AnalyticsService(db_session)
        result = await service._freshness_from_pg()

        assert result["total_documents"] == 1
        assert result["expired"] == 1
        assert result["freshness_rate"] == 0.0
        assert result["source"] == "postgresql"

    async def test_freshness_pg_fallback_when_no_tenant(self, db_session):
        """无租户时 get_knowledge_freshness 应走 PG 降级路径。

        无租户 → TenantService.is_module_enabled 返回 False
        （knowledge_graph 不是基础模块）→ 走 _freshness_from_pg
        """
        service = AnalyticsService(db_session)
        result = await service.get_knowledge_freshness()

        assert result["source"] == "postgresql"
        assert "total_documents" in result
        assert "expired" in result
        assert "freshness_rate" in result

    async def test_freshness_pg_fallback_with_tenant_no_graph(
        self, db_session
    ):
        """租户未启用 knowledge_graph → 走 PG 降级。"""
        from app.models.billing import Tenant
        from app.services.tenant_service import TenantService

        # 创建 free 套餐租户（不含 knowledge_graph）
        tenant = Tenant(name="免费租户", plan="free")
        db_session.add(tenant)
        await db_session.flush()

        # 创建文档
        owner = uuid.uuid4()
        kb = KnowledgeBase(name="KB", owner_id=owner)
        db_session.add(kb)
        await db_session.flush()
        doc = Document(
            kb_id=kb.id,
            title="文档",
            content_html="<p>x</p>",
            doc_type="md",
            category="技术",
            owner_id=owner,
        )
        db_session.add(doc)
        await db_session.flush()

        service = AnalyticsService(db_session)
        result = await service.get_knowledge_freshness(tenant_id=tenant.id)

        assert result["source"] == "postgresql"
        assert result["total_documents"] == 1
        assert result["expired"] == 0

    async def test_freshness_pg_fallback_with_tenant_graph_enabled(
        self, db_session
    ):
        """租户启用了 knowledge_graph 但无 Neo4j → GraphitiManager 返回空数据。

        GraphitiManager 在无 Neo4j 环境下不会抛异常，
        而是返回空列表（优雅降级），因此 source 为 "graphiti"。
        如果 GraphitiManager 抛异常，则降级到 "postgresql"。
        两种情况都是可接受的。
        """
        from app.models.billing import Tenant

        # 创建 enterprise 租户并启用 knowledge_graph
        tenant = Tenant(
            name="企业租户",
            plan="enterprise",
            settings={"enabled_modules": ["knowledge_graph"]},
        )
        db_session.add(tenant)
        await db_session.flush()

        owner = uuid.uuid4()
        kb = KnowledgeBase(name="KB", owner_id=owner)
        db_session.add(kb)
        await db_session.flush()
        doc = Document(
            kb_id=kb.id,
            title="文档",
            content_html="<p>x</p>",
            doc_type="md",
            category="技术",
            owner_id=owner,
        )
        db_session.add(doc)
        await db_session.flush()

        service = AnalyticsService(db_session)
        result = await service.get_knowledge_freshness(tenant_id=tenant.id)

        # GraphitiManager 无 Neo4j 时返回空列表（不抛异常），source 为 graphiti
        # 或抛异常时降级到 postgresql — 两种均可接受
        assert result["source"] in ("graphiti", "postgresql")
        assert result["total_documents"] == 1
        assert "freshness_rate" in result
