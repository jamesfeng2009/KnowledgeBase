"""B5 验收闸门 — event_log.append 失败不得污染共享事务；seq 竞态不得丢事件。

审计证据：
  - append 失败仅 return 0，无 savepoint：asyncpg 报错后 PG 事务已中止，
    共享 session 的后续操作全部 PendingRollbackError；
  - seq 用 INSERT 内 MAX+1 计算，并发下唯一约束拒绝重复 seq，
    注释声称「可重试」但实现没有任何重试 → 事件静默丢失。

基座：conftest db_session（真实 PostgreSQL）。(session_id, seq) 唯一索引
只存在于生产迁移 bd2c3d4e5f70（ORM 未声明），这里从迁移本体同源执行，
不手写副本。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
async def event_seq_index(db_session):
    """从生产迁移本体建 (session_id, seq) 唯一索引（幂等 DDL）。"""
    hits = sorted(
        (BACKEND_DIR / "alembic" / "versions").glob("*add_agent_event_logs*.py")
    )
    assert hits, "缺少 agent_event_logs 迁移"
    spec = importlib.util.spec_from_file_location("b5_event_log_migration", hits[-1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _run_upgrade(sync_conn):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        ctx = MigrationContext.configure(sync_conn)
        ops = Operations(ctx)
        ops._install_proxy()
        try:
            mod.upgrade()
        finally:
            ops._remove_proxy()

    conn = await db_session.connection()
    await conn.run_sync(_run_upgrade)
    await db_session.commit()


class TestB5SavepointIsolation:
    async def test_failed_append_does_not_poison_shared_session(
        self, db_session, event_seq_index
    ):
        """append 失败（VARCHAR(64) 溢出）返回 0 后，同一 session 必须仍可写。"""
        from app.memory.event_log import EventLogManager

        mgr = EventLogManager(db_session)
        bad_sid = "x" * 65  # 超出 session_id VARCHAR(64) → 真实 DB 报错
        assert await mgr.append(bad_sid, "node_end", "think") == 0

        # 正向证人：共享事务未被中止，普通查询与后续 append 都能成功
        assert (await db_session.execute(sa.text("SELECT 1"))).scalar() == 1
        good_sid = str(uuid.uuid4())
        assert await mgr.append(good_sid, "node_end", "think") == 1
        await db_session.commit()
        assert await mgr.get_event_count(good_sid) == 1


class TestB5SeqRace:
    async def test_concurrent_appends_lose_no_events(
        self, db_session, event_seq_index
    ):
        """8 路并发 append 同一 session_id：一条不丢，seq 恰好 1..8。"""
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import NullPool

        from app.memory.event_log import EventLogManager

        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        sid = str(uuid.uuid4())

        async def _one(i: int) -> int:
            async with factory() as s:
                seq = await EventLogManager(s).append(sid, "node_end", f"n{i}")
                await s.commit()
                return seq

        try:
            seqs = await asyncio.gather(*(_one(i) for i in range(8)))
            assert all(sq > 0 for sq in seqs), f"有事件被丢失: {seqs}"

            async with factory() as s:
                mgr = EventLogManager(s)
                assert await mgr.get_event_count(sid) == 8
                records = await mgr.list_after(sid)
                assert sorted(r.seq for r in records) == list(range(1, 9))
                await mgr.delete_all(sid)
                await s.commit()
        finally:
            await engine.dispose()