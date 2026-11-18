"""B9 验收闸门 — 用户纠错/偏好管理必须有生产入口，清理职责必须统一。

审计证据：correct_fact / set_preference / clear_working_memory /
Mem0Manager.cleanup_expired / sync_to_graph 全部无生产调用方；
Graphiti 偏好时间线永不写入；定时任务自写一份清理 SQL，缺退场语义
（forgotten_reason / 激活值快照）且按租户迭代漏掉单租户 NULL 行。

决策（用户已确认）：补 memory API 路由 + 定时任务改调
Mem0Manager.cleanup_expired + sync_to_graph 挂 record_event 后 best-effort。

基座：C1 rls_harness；API 走 ASGITransport + 依赖覆盖（真实 PG 会话）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from tests.rls_harness import rls_db  # noqa: F401

TENANT_A = "aaaaaaaa-0000-4000-8000-00000000000a"


@pytest.fixture(autouse=True)
async def _clean(rls_db):
    yield
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        await conn.execute(
            "TRUNCATE memory_facts, graphiti_entities, graphiti_events, "
            "agent_checkpoints, agent_event_logs, conversations, users, "
            "tenants CASCADE"
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch):
    from app.models.memory import MemoryFact

    vec_dim = MemoryFact.__table__.c.embedding_vec.type.dim

    class _StubEmbedder:
        dim = vec_dim

        async def embed(self, texts):
            return [[0.1] * vec_dim for _ in texts]

    import app.llm.factory as factory

    monkeypatch.setattr(factory, "get_embedder", lambda: _StubEmbedder())


async def _owner_exec(rls_db, sql: str, *args):
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _seed_user(rls_db, tenant_id: str | None = TENANT_A):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.user import User

    url = rls_db.owner_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    try:
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            if tenant_id:
                # users.tenant_id 外键要求租户行先存在
                from sqlalchemy import text

                await s.execute(
                    text(
                        "INSERT INTO tenants (id, name, plan, max_users, max_storage) "
                        "VALUES (:tid, 'B9租户', 'free', 10, 1073741824) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"tid": tenant_id},
                )
            user = User(
                email=f"b9-{uuid.uuid4()}@ekb.local",
                hashed_password="x",
                name="B9",
                tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
            )
            s.add(user)
            await s.commit()
            return user
    finally:
        await engine.dispose()


async def _seed_fact(rls_db, user_id, category="working", tenant_id=TENANT_A,
                     expired=False, fact_text="待处理事实"):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.memory import MemoryFact

    url = rls_db.owner_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    try:
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            fact = MemoryFact(
                user_id=user_id,
                fact_text=fact_text,
                category=category,
                tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
                is_active=True,
                expires_at=(
                    datetime.now(timezone.utc) - timedelta(hours=1)
                    if expired else None
                ),
            )
            s.add(fact)
            await s.commit()
            return fact.id
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def api_client(rls_db):
    """认证 + 真实 PG 会话覆盖的客户端（租户 A GUC，与生产请求同边界）。"""
    from app.core.tenant_context import tenant_scope
    from app.database import get_db_session
    from app.deps import get_current_active_user
    from app.main import app

    user = await _seed_user(rls_db)
    async with rls_db.factory() as session:
        async with tenant_scope(session, TENANT_A):
            async def override_user():
                return user

            async def override_db():
                yield session

            app.dependency_overrides[get_current_active_user] = override_user
            app.dependency_overrides[get_db_session] = override_db
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                yield client, user, session
            app.dependency_overrides.clear()


class TestB9MemoryApi:
    async def test_set_preference_writes_fact_and_graphiti_timeline(
        self, rls_db, api_client
    ):
        """偏好设置入口：Mem0 事实 + Graphiti 时间线都必须真实写入。"""
        client, user, session = api_client
        resp = await client.put(
            "/api/v1/memory/preferences",
            json={"key": "answer_style", "value": "简洁"},
        )
        assert resp.status_code == 200, f"偏好入口必须存在：{resp.status_code} {resp.text}"
        await session.commit()

        facts = await _owner_exec(
            rls_db,
            "SELECT fact_key, fact_value, tenant_id FROM memory_facts "
            "WHERE user_id = $1 AND category = 'preference' AND is_active = true",
            user.id,
        )
        assert len(facts) == 1
        assert facts[0]["fact_key"] == "answer_style"
        assert facts[0]["fact_value"] == "简洁"
        assert str(facts[0]["tenant_id"]) == TENANT_A

        events = await _owner_exec(
            rls_db,
            "SELECT e.event_type, e.new_value FROM graphiti_events e "
            "JOIN graphiti_entities en ON en.id = e.entity_id "
            "WHERE en.entity_type = 'user_preference' AND en.entity_ref_id = $1",
            user.id,
        )
        assert len(events) == 1, "Graphiti 偏好时间线必须被写入（审计现状：永不写入）"
        assert events[0]["event_type"] == "preference_changed"
        assert events[0]["new_value"] == "简洁"

    async def test_correct_fact_deactivates_old_and_writes_new(
        self, rls_db, api_client
    ):
        """纠错入口：旧事实停用（corrected 退场）+ 新事实生效。"""
        client, user, session = api_client
        fact_id = await _seed_fact(
            rls_db, user.id, category="preference", fact_text="报销上限: 500"
        )
        resp = await client.post(
            f"/api/v1/memory/facts/{fact_id}/correct",
            json={"corrected_text": "报销上限: 800", "corrected_value": "800"},
        )
        assert resp.status_code == 200, f"纠错入口必须存在：{resp.status_code} {resp.text}"
        await session.commit()

        rows = await _owner_exec(
            rls_db,
            "SELECT id, is_active, forgotten_reason, fact_text FROM memory_facts "
            "WHERE user_id = $1 ORDER BY created_at",
            user.id,
        )
        assert len(rows) == 2
        old = next(r for r in rows if r["id"] == fact_id)
        assert old["is_active"] is False
        assert old["forgotten_reason"] == "corrected"
        new = next(r for r in rows if r["id"] != fact_id)
        assert new["is_active"] is True
        assert new["fact_text"] == "报销上限: 800"

    async def test_correct_fact_rejects_other_users_fact(self, rls_db, api_client):
        """属主校验：不得纠正同租户其他用户的事实。"""
        client, user, session = api_client
        other = await _seed_user(rls_db)
        other_fact = await _seed_fact(
            rls_db, other.id, category="preference", fact_text="别人的偏好"
        )
        resp = await client.post(
            f"/api/v1/memory/facts/{other_fact}/correct",
            json={"corrected_text": "越权篡改"},
        )
        assert resp.status_code == 404
        rows = await _owner_exec(
            rls_db,
            "SELECT is_active, fact_text FROM memory_facts WHERE id = $1",
            other_fact,
        )
        assert rows[0]["is_active"] is True, "越权纠错不得改动他人事实"
        assert rows[0]["fact_text"] == "别人的偏好"

    async def test_clear_working_memory_endpoint(self, rls_db, api_client):
        """清空工作记忆入口：working 类事实停用，偏好不受影响。"""
        client, user, session = api_client
        await _seed_fact(rls_db, user.id, category="working", fact_text="任务A")
        await _seed_fact(rls_db, user.id, category="working", fact_text="任务B")
        await _seed_fact(rls_db, user.id, category="preference", fact_text="长期偏好")

        resp = await client.delete("/api/v1/memory/working-memory")
        assert resp.status_code == 200, f"清空入口必须存在：{resp.status_code} {resp.text}"
        await session.commit()

        working = await _owner_exec(
            rls_db,
            "SELECT is_active FROM memory_facts "
            "WHERE user_id = $1 AND category = 'working'",
            user.id,
        )
        assert len(working) == 2
        assert all(r["is_active"] is False for r in working)
        pref = await _owner_exec(
            rls_db,
            "SELECT is_active FROM memory_facts "
            "WHERE user_id = $1 AND category = 'preference'",
            user.id,
        )
        assert pref[0]["is_active"] is True, "长期偏好不随工作记忆清空"


class TestB9CleanupUnification:
    async def test_scheduled_cleanup_delegates_with_retirement_semantics(
        self, rls_db, monkeypatch
    ):
        """定时清理必须走 Mem0Manager.cleanup_expired：退场语义 + NULL 行覆盖。"""
        import app.database as database

        monkeypatch.setattr(database.settings, "DATABASE_URL", rls_db.app_url)

        user_a = await _seed_user(rls_db, TENANT_A)
        user_null = await _seed_user(rls_db, None)
        fact_a = await _seed_fact(
            rls_db, user_a.id, expired=True, fact_text="租户A过期"
        )
        fact_null = await _seed_fact(
            rls_db, user_null.id, tenant_id=None, expired=True, fact_text="单租户过期"
        )
        fact_live = await _seed_fact(
            rls_db, user_a.id, expired=False, fact_text="未过期"
        )

        from tasks.scheduled_tasks import _cleanup_expired_facts_async

        result = await _cleanup_expired_facts_async()
        assert result["status"] == "success"
        assert result["cleaned_count"] == 2, (
            "租户行与单租户 NULL 行都必须被清理（审计现状：NULL 行漏清）"
        )

        rows = await _owner_exec(
            rls_db,
            "SELECT id, is_active, forgotten_reason, activation_value "
            "FROM memory_facts WHERE id = ANY($1::uuid[])",
            [fact_a, fact_null, fact_live],
        )
        by_id = {r["id"]: r for r in rows}
        for fid in (fact_a, fact_null):
            assert by_id[fid]["is_active"] is False
            assert by_id[fid]["forgotten_reason"] == "expired", (
                "清理必须带退场语义（审计现状：定时任务自写 SQL 缺 forgotten_reason）"
            )
            assert by_id[fid]["activation_value"] is not None
        assert by_id[fact_live]["is_active"] is True


class TestB9SyncToGraphWiring:
    async def test_record_event_triggers_graph_sync(self, rls_db):
        """sync_to_graph 必须挂进实体变更链路（Neo4j 可选，best-effort）。"""
        from app.core.tenant_context import tenant_scope
        from app.memory.graphiti_manager import GraphitiManager

        graph_spy = MagicMock()
        graph_spy.create_node = AsyncMock(return_value=True)
        graph_spy.create_relationship = AsyncMock(return_value=True)

        async with rls_db.factory() as session:
            async with tenant_scope(session, TENANT_A):
                mgr = GraphitiManager(session, tenant_id=uuid.UUID(TENANT_A))
                entity = await mgr.register_entity(
                    entity_type="policy", name="报销制度", version="v1"
                )
                with patch(
                    "app.services.graph_service.get_graph_service",
                    return_value=graph_spy,
                ):
                    await mgr.record_event(
                        entity_id=entity.id,
                        event_type="version_updated",
                        old_value="v1",
                        new_value="v2",
                    )
            await session.commit()

        assert graph_spy.create_node.await_count >= 2, (
            "实体变更后必须尝试同步 Neo4j（实体节点 + 事件节点；审计现状：零调用）"
        )
        labels = [c.args[0] for c in graph_spy.create_node.await_args_list]
        assert "KnowledgeEntity" in labels
        assert "TimelineEvent" in labels