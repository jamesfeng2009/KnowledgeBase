"""B3 验收闸门 — 单租户（tenant_id=NULL）会话的异步记忆写入不得被误判跳过。

审计证据：memory_tasks 用 resolve_tenant_of_row 拿不到租户（NULL）后，
 existence 检查在 fail-closed 无 GUC 下恒空 → 按「会话已删除」跳过写入。
修复必须区分「会话不存在/已删除」与「单租户 NULL」，后者正常写入。

基座：C1 rls_harness（真实 RLS + 非 superuser 运行角色）。
LLM 事实提取属外部边界，按 B1 同款方式打桩；save_session / add_fact /
checkpoint 写入全部走真实 DB。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from tests.rls_harness import rls_db  # noqa: F401

TENANT_A = "aaaaaaaa-0000-4000-8000-00000000000a"


@pytest.fixture(autouse=True)
async def _clean(rls_db):
    yield
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        await conn.execute(
            "TRUNCATE memory_facts, agent_checkpoints, agent_event_logs, "
            "conversations, users CASCADE"
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _task_engine_uses_app_role(rls_db, monkeypatch):
    """task_db_session 默认绑 settings.DATABASE_URL（测试进程里是 superuser，
    会无条件绕过 RLS，把 B3 缺陷掩盖掉）。这里改绑运行角色连接串，
    让任务体打到与生产一致的 fail-closed 边界。"""
    import app.database as database

    monkeypatch.setattr(database.settings, "DATABASE_URL", rls_db.app_url)


@pytest.fixture(autouse=True)
def _stub_llm_boundaries(monkeypatch):
    """LLM 提取打桩（外部边界）；embedder 用与列宽同维的确定性向量。"""
    from app.memory.memory_manager import MemoryManager
    from app.models.memory import MemoryFact

    vec_dim = MemoryFact.__table__.c.embedding_vec.type.dim

    class _StubEmbedder:
        dim = vec_dim

        async def embed(self, texts):
            return [[0.1] * vec_dim for _ in texts]

    import app.llm.factory as factory

    monkeypatch.setattr(factory, "get_embedder", lambda: _StubEmbedder())
    monkeypatch.setattr(
        MemoryManager, "extract_and_save_facts", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        MemoryManager, "extract_and_save_key_decisions", AsyncMock(return_value=None)
    )


async def _seed_conversation(rls_db, tenant_id: str | None, deleted: bool = False):
    """owner 身份种一个用户 + 会话（superuser 绕过 RLS，只用于准备数据）。"""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.conversation import Conversation
    from app.models.user import User

    url = rls_db.owner_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    try:
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            user = User(
                email=f"b3-{uuid.uuid4()}@ekb.local",
                hashed_password="x",
                name="B3",
            )
            s.add(user)
            await s.flush()
            conv = Conversation(
                user_id=user.id,
                tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
            )
            if deleted:
                from datetime import datetime, timezone

                conv.deleted_at = datetime.now(timezone.utc)
            s.add(conv)
            await s.commit()
            return str(user.id), str(conv.id)
    finally:
        await engine.dispose()


async def _owner_rows(rls_db, sql: str, *args):
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


class TestB3SingleTenantNull:
    async def test_null_tenant_conversation_memory_written(self, rls_db):
        """单租户 NULL 会话：记忆必须正常落库，不得按「会话已删除」跳过。"""
        from tasks.memory_tasks import _persist_memory_async

        user_id, session_id = await _seed_conversation(rls_db, tenant_id=None)
        result = await _persist_memory_async(
            user_id=user_id,
            session_id=session_id,
            query="单租户提问",
            summary="单租户会话摘要",
        )
        assert result["session_saved"] is True

        rows = await _owner_rows(
            rls_db,
            "SELECT tenant_id, fact_text FROM memory_facts "
            "WHERE category = 'summary' AND fact_text = '单租户会话摘要'",
        )
        assert len(rows) == 1
        assert rows[0]["tenant_id"] is None

    async def test_tenant_conversation_memory_still_isolated(self, rls_db):
        """正向证人：有租户的会话照旧写入本租户（既有行为不回归）。"""
        from tasks.memory_tasks import _persist_memory_async

        user_id, session_id = await _seed_conversation(rls_db, tenant_id=TENANT_A)
        result = await _persist_memory_async(
            user_id=user_id,
            session_id=session_id,
            query="租户提问",
            summary="租户会话摘要",
        )
        assert result["session_saved"] is True
        rows = await _owner_rows(
            rls_db,
            "SELECT tenant_id FROM memory_facts WHERE fact_text = '租户会话摘要'",
        )
        assert len(rows) == 1
        assert str(rows[0]["tenant_id"]) == TENANT_A

    async def test_deleted_conversation_still_skipped(self, rls_db):
        """反向证人：软删除会话仍须跳过（P1b 竞态守卫不回归）。"""
        from tasks.memory_tasks import _persist_memory_async

        user_id, session_id = await _seed_conversation(
            rls_db, tenant_id=TENANT_A, deleted=True
        )
        result = await _persist_memory_async(
            user_id=user_id,
            session_id=session_id,
            query="已删除会话",
            summary="不应落库的摘要",
        )
        assert result["session_saved"] is False
        rows = await _owner_rows(
            rls_db,
            "SELECT 1 FROM memory_facts WHERE fact_text = '不应落库的摘要'",
        )
        assert rows == []

    async def test_missing_conversation_still_skipped(self, rls_db):
        """反向证人：不存在的会话仍须跳过。"""
        from tasks.memory_tasks import _persist_memory_async

        result = await _persist_memory_async(
            user_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
            query="不存在会话",
            summary="也不应落库",
        )
        assert result["session_saved"] is False
        rows = await _owner_rows(
            rls_db, "SELECT 1 FROM memory_facts WHERE fact_text = '也不应落库'"
        )
        assert rows == []
