"""B1 验收闸门 — 记忆写入租户隔离（真实 RLS，非 create_all 影子）。

基座见 rls_harness.py：生产 ORM 建表 + 生产迁移 e8f9a0b1c2d3 的 fail-closed
策略 + 非 superuser/NOBYPASSRLS 运行角色。GUC 一律走生产 tenant_context API。

红基线（修复前）：add_fact/register_entity 不填 tenant_id → 写入行
tenant_id=NULL → fail-closed WITH CHECK 拒绝 → 写入直接抛错；
即便写进去（旧 fail-open），读取侧也按租户过滤恒空。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from tests.rls_harness import rls_db  # noqa: F401 — 模块级 fixture 注入

TENANT_A = "aaaaaaaa-0000-4000-8000-00000000000a"
TENANT_B = "bbbbbbbb-0000-4000-8000-00000000000b"


class _StubEmbedder:
    """替身 embedder — 维度取真实列宽，避免测试发起真实向量化请求。"""

    def __init__(self, dim: int):
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dim for _ in texts]


def _stub_dim() -> int:
    from app.models.memory import MemoryFact

    return MemoryFact.__table__.c.embedding_vec.type.dim


@asynccontextmanager
async def _tenant_session(factory, tenant: str):
    from app.core.tenant_context import tenant_scope

    async with factory() as session:
        async with tenant_scope(session, tenant):
            yield session
            await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_tables(rls_db):
    """每个用例后清空记忆三表（owner 身份，superuser 绕过 RLS 属预期）。"""
    yield
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        await conn.execute(
            "TRUNCATE memory_facts, graphiti_entities, graphiti_events CASCADE"
        )
    finally:
        await conn.close()


class TestB1FactTenantIsolation:
    async def test_add_fact_fills_tenant_and_reads_back(self, rls_db):
        """正向证人：租户 A 写入成功、tenant_id 落库、A 自己能读到。"""
        from app.memory.mem0_manager import Mem0Manager

        user_id = uuid.uuid4()
        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mgr = Mem0Manager(s)
            mgr._embedder = _StubEmbedder(_stub_dim())
            fact = await mgr.add_fact(user_id=user_id, fact_text="用户偏好简洁回答")
            assert fact.tenant_id == uuid.UUID(TENANT_A)

        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mgr = Mem0Manager(s)
            facts = await mgr.search_facts(user_id=user_id)
            assert [f.fact_text for f in facts] == ["用户偏好简洁回答"]

    async def test_other_tenant_cannot_read(self, rls_db):
        """核心断言：租户 A 的记忆，租户 B 一行都看不到（同一 user_id 也不行）。"""
        from app.memory.mem0_manager import Mem0Manager
        from app.models.memory import MemoryFact

        user_id = uuid.uuid4()
        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mgr = Mem0Manager(s)
            mgr._embedder = _StubEmbedder(_stub_dim())
            await mgr.add_fact(user_id=user_id, fact_text="租户A的私密事实")

        async with _tenant_session(rls_db.factory, TENANT_B) as s:
            mgr = Mem0Manager(s)
            assert await mgr.search_facts(user_id=user_id) == []
            count = (
                await s.execute(select(MemoryFact).where(MemoryFact.user_id == user_id))
            ).scalars().all()
            assert count == []

    async def test_write_without_guc_is_rejected_loudly(self, rls_db):
        """fail-closed：无租户身份写入必须抛错（不得静默成功/静默失败）。"""
        from app.memory.mem0_manager import Mem0Manager

        async with rls_db.factory() as s:
            mgr = Mem0Manager(s)
            mgr._embedder = _StubEmbedder(_stub_dim())
            with pytest.raises(DBAPIError):
                await mgr.add_fact(user_id=uuid.uuid4(), fact_text="无身份写入")

    async def test_constructor_tenant_propagates_via_memory_manager(self, rls_db):
        """MemoryManager(tenant_id=…) 必须把租户传到 mem0/graphiti 写入。"""
        from app.memory.memory_manager import MemoryManager
        from app.models.memory import MemoryFact

        user_id = uuid.uuid4()
        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mm = MemoryManager(s, tenant_id=uuid.UUID(TENANT_A))
            mm.mem0._embedder = _StubEmbedder(_stub_dim())
            await mm.save_session(
                user_id=user_id,
                session_id=str(uuid.uuid4()),
                agent_state={"iteration": 0},
                summary="讨论了记忆模块修复方案",
            )
            rows = (
                await s.execute(select(MemoryFact).where(MemoryFact.user_id == user_id))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].tenant_id == uuid.UUID(TENANT_A)

    async def test_guc_only_path_fills_tenant(self, rls_db):
        """memory_tasks 路径：MemoryManager 未传 tenant_id，靠会话 GUC 解析。"""
        from app.memory.memory_manager import MemoryManager
        from app.models.memory import MemoryFact

        user_id = uuid.uuid4()
        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mm = MemoryManager(s)  # 不传 tenant_id —— GUC 已由 tenant_scope 设置
            mm.mem0._embedder = _StubEmbedder(_stub_dim())
            await mm.update_working_memory(user_id, "报销单号", "BG2024001")
            rows = (
                await s.execute(select(MemoryFact).where(MemoryFact.user_id == user_id))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].tenant_id == uuid.UUID(TENANT_A)


class TestB1GraphitiTenantIsolation:
    async def test_entity_and_event_fill_tenant_and_isolate(self, rls_db):
        """register_entity/record_event 落租户；B 租户看不到 A 的实体与时间线。"""
        from app.memory.graphiti_manager import GraphitiManager
        from app.models.memory import KnowledgeEntity

        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mgr = GraphitiManager(s)
            entity = await mgr.register_entity(
                entity_type="policy", name="报销制度", version="v1"
            )
            assert entity.tenant_id == uuid.UUID(TENANT_A)
            event = await mgr.record_event(
                entity_id=entity.id,
                event_type="version_updated",
                old_value="v1",
                new_value="v2",
            )
            assert event.tenant_id == uuid.UUID(TENANT_A)
            entity_id = entity.id

        async with _tenant_session(rls_db.factory, TENANT_B) as s:
            mgr = GraphitiManager(s)
            assert await mgr.get_entity_timeline(entity_id) == []
            rows = (
                await s.execute(
                    select(KnowledgeEntity).where(KnowledgeEntity.id == entity_id)
                )
            ).scalars().all()
            assert rows == []

        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mgr = GraphitiManager(s)
            timeline = await mgr.get_entity_timeline(entity_id)
            assert len(timeline) == 1
            assert timeline[0].new_value == "v2"

    async def test_preference_change_path_fills_tenant(self, rls_db):
        """偏好变更 → Mem0 事实 + Graphiti 偏好实体/事件，全链路带租户。"""
        from app.memory.memory_manager import MemoryManager
        from app.models.memory import KnowledgeEntity, MemoryFact

        user_id = uuid.uuid4()
        async with _tenant_session(rls_db.factory, TENANT_A) as s:
            mm = MemoryManager(s, tenant_id=uuid.UUID(TENANT_A))
            mm.mem0._embedder = _StubEmbedder(_stub_dim())
            await mm.set_preference(user_id, "answer_style", "concise")

            facts = (
                await s.execute(select(MemoryFact).where(MemoryFact.user_id == user_id))
            ).scalars().all()
            assert facts and all(f.tenant_id == uuid.UUID(TENANT_A) for f in facts)
            entities = (
                await s.execute(
                    select(KnowledgeEntity).where(
                        KnowledgeEntity.entity_ref_id == user_id
                    )
                )
            ).scalars().all()
            assert entities and all(
                e.tenant_id == uuid.UUID(TENANT_A) for e in entities
            )