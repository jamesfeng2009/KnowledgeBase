"""B6 验收闸门 — agent_checkpoints / agent_event_logs 必须有 tenant 列 + RLS。

审计证据：两表无 tenant 列无 RLS，隔离全靠上游归属校验
（chat_service.py 的 conversation.user_id 检查）。任何拿到别租户
session_id 的调用方直调 load_checkpoint / list_after 即可越权读状态。

基座：C1 rls_harness（生产迁移策略 SQL + 非 superuser 运行角色）。
红基线（修复前）：跨租户 load_checkpoint 能读到别租户状态；
写入行无 tenant 列可填；无 GUC 写入不被拒绝。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.tenant_context import tenant_scope
from tests.rls_harness import rls_db  # noqa: F401

TENANT_A = "aaaaaaaa-0000-4000-8000-00000000000a"
TENANT_B = "bbbbbbbb-0000-4000-8000-00000000000b"


@pytest.fixture(autouse=True)
async def _clean_state_tables(rls_db):
    yield
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        await conn.execute("TRUNCATE agent_checkpoints, agent_event_logs CASCADE")
    finally:
        await conn.close()


async def _owner_fetch(rls_db, sql: str, *args):
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


class TestB6CheckpointRls:
    async def test_cross_tenant_load_checkpoint_denied(self, rls_db):
        """审计验证方法：跨租户 session_id 直调 load_checkpoint 不得越权。"""
        from app.memory.checkpoint import CheckpointManager

        sid = str(uuid.uuid4())
        state = {"iteration": 3, "retrieved_docs": ["doc-1", "doc-2"]}
        async with rls_db.factory() as s:
            async with tenant_scope(s, TENANT_A):
                await CheckpointManager(s).save_checkpoint(sid, state, 3)
            await s.commit()

        # 写入侧确实填了租户（owner 直读，与 RLS 读路径不同源）
        rows = await _owner_fetch(
            rls_db,
            "SELECT tenant_id FROM agent_checkpoints WHERE session_id = $1",
            sid,
        )
        assert len(rows) == 1
        assert str(rows[0]["tenant_id"]) == TENANT_A

        # 租户 B 直调 load_checkpoint → 必须读不到
        async with rls_db.factory() as s:
            async with tenant_scope(s, TENANT_B):
                assert await CheckpointManager(s).load_checkpoint(sid) is None

        # 正向证人：租户 A 自己能读到完整状态
        async with rls_db.factory() as s:
            async with tenant_scope(s, TENANT_A):
                loaded = await CheckpointManager(s).load_checkpoint(sid)
        assert loaded is not None
        assert loaded["iteration"] == 3
        assert loaded["retrieved_docs"] == ["doc-1", "doc-2"]

    async def test_checkpoint_write_without_guc_rejected(self, rls_db):
        """fail-closed 证人：无 GUC 的写入必须被 RLS 响亮拒绝。"""
        from app.memory.checkpoint import CheckpointManager

        async with rls_db.factory() as s:
            with pytest.raises(DBAPIError):
                await CheckpointManager(s).save_checkpoint(
                    str(uuid.uuid4()), {"iteration": 1}, 1
                )


class TestB6EventLogRls:
    async def test_cross_tenant_event_log_denied(self, rls_db):
        from app.memory.event_log import EventLogManager

        sid = str(uuid.uuid4())
        async with rls_db.factory() as s:
            async with tenant_scope(s, TENANT_A):
                mgr = EventLogManager(s)
                seq = await mgr.append(
                    session_id=sid,
                    event_type="node_end",
                    node_name="think",
                    output_data={"messages": ["m1"]},
                    iteration=1,
                )
            await s.commit()
        assert seq >= 1

        rows = await _owner_fetch(
            rls_db,
            "SELECT tenant_id FROM agent_event_logs WHERE session_id = $1",
            sid,
        )
        assert len(rows) == 1
        assert str(rows[0]["tenant_id"]) == TENANT_A

        # 租户 B 直调 list_after → 空
        async with rls_db.factory() as s:
            async with tenant_scope(s, TENANT_B):
                assert await EventLogManager(s).list_after(sid) == []

        # 正向证人：租户 A 能读回自己的事件
        async with rls_db.factory() as s:
            async with tenant_scope(s, TENANT_A):
                events = await EventLogManager(s).list_after(sid)
        assert [e.seq for e in events] == [seq]
        assert events[0].output_data == {"messages": ["m1"]}

    async def test_event_log_write_without_guc_not_stored(self, rls_db):
        """无 GUC 时 append 不得落库（吞错返回 0 是 B5 契约，但行必须写不进）。"""
        from app.memory.event_log import EventLogManager

        sid = str(uuid.uuid4())
        async with rls_db.factory() as s:
            seq = await EventLogManager(s).append(
                session_id=sid, event_type="node_end", node_name="think"
            )
        assert seq == 0
        rows = await _owner_fetch(
            rls_db,
            "SELECT 1 FROM agent_event_logs WHERE session_id = $1",
            sid,
        )
        assert rows == []
