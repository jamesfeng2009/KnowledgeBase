"""B6 迁移闸门 — 存量 agent_checkpoints / agent_event_logs 补 tenant 列 + RLS。

真实 PG 执行生产迁移 upgrade：
1. 两表补出 tenant_id 列；
2. 存量行按 session_id → conversations.id 回填租户（task:* 键无会话归属，留 NULL）；
3. ENABLE + FORCE RLS + fail-closed tenant_isolation 策略。

断言两侧不同源：期望租户来自测试插入的 conversations 行，
实际值读自迁移后的 information_schema / pg_policies / 表数据。
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _load_migration(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def migration():
    hits = sorted(
        (BACKEND_DIR / "alembic" / "versions").glob("*add_tenant_rls_agent_state*.py")
    )
    assert hits, "缺少 B6 迁移（*add_tenant_rls_agent_state*.py）"
    return _load_migration("b6_agent_state_migration", hits[-1])


class TestB6AgentStateMigration:
    async def test_upgrade_backfills_tenant_and_enables_rls(
        self, db_session, migration
    ):
        from app.models.conversation import Conversation
        from app.models.user import User

        tid_a = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
        tid_b = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")
        user = User(
            email=f"b6-{uuid.uuid4()}@ekb.local",
            hashed_password="x",
            name="B6",
        )
        db_session.add(user)
        await db_session.flush()
        conv_a = Conversation(user_id=user.id, tenant_id=tid_a)
        conv_b = Conversation(user_id=user.id, tenant_id=tid_b)
        db_session.add_all([conv_a, conv_b])
        await db_session.flush()
        sid_a, sid_b = str(conv_a.id), str(conv_b.id)

        # 模拟存量旧 schema：无 tenant 列（修复前 ORM 本就没有该列）
        await db_session.execute(
            sa.text("ALTER TABLE agent_checkpoints DROP COLUMN IF EXISTS tenant_id")
        )
        await db_session.execute(
            sa.text("ALTER TABLE agent_event_logs DROP COLUMN IF EXISTS tenant_id")
        )
        await db_session.execute(
            sa.text(
                "INSERT INTO agent_checkpoints (session_id, agent_state, iteration) "
                "VALUES (:a, '{\"iteration\": 1}'::jsonb, 1), "
                "(:b, '{\"iteration\": 2}'::jsonb, 2), "
                "('task:no-conversation', '{}'::jsonb, 0)"
            ),
            {"a": sid_a, "b": sid_b},
        )
        await db_session.execute(
            sa.text(
                "INSERT INTO agent_event_logs "
                "(session_id, seq, event_type, node_name, iteration) "
                "VALUES (:a, 1, 'node_end', 'think', 1), "
                "(:b, 1, 'node_end', 'think', 1)"
            ),
            {"a": sid_a, "b": sid_b},
        )
        await db_session.commit()

        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            def _run_upgrade(sync_conn):
                from alembic.migration import MigrationContext
                from alembic.operations import Operations

                ctx = MigrationContext.configure(sync_conn)
                ops = Operations(ctx)
                ops._install_proxy()
                try:
                    migration.upgrade()
                finally:
                    ops._remove_proxy()

            async with engine.begin() as conn:
                await conn.run_sync(_run_upgrade)

            async with engine.begin() as conn:
                for table in ("agent_checkpoints", "agent_event_logs"):
                    col = (
                        await conn.execute(
                            sa.text(
                                "SELECT data_type FROM information_schema.columns "
                                "WHERE table_name = :t AND column_name = 'tenant_id'"
                            ),
                            {"t": table},
                        )
                    ).scalar()
                    assert col == "uuid", f"{table} 缺 tenant_id uuid 列"

                    policy = (
                        await conn.execute(
                            sa.text(
                                "SELECT qual FROM pg_policies "
                                "WHERE tablename = :t AND policyname = 'tenant_isolation'"
                            ),
                            {"t": table},
                        )
                    ).scalar()
                    assert policy and "app.tenant_id" in policy, (
                        f"{table} 缺 fail-closed tenant_isolation 策略"
                    )
                    forced = (
                        await conn.execute(
                            sa.text(
                                "SELECT relforcerowsecurity FROM pg_class "
                                "WHERE relname = :t"
                            ),
                            {"t": table},
                        )
                    ).scalar()
                    assert forced is True, f"{table} 未 FORCE RLS（owner 可绕过）"

                # 回填断言：期望值来自插入的会话租户，实际值读自迁移后的表
                rows = (
                    await conn.execute(
                        sa.text(
                            "SELECT session_id, tenant_id FROM agent_checkpoints"
                        )
                    )
                ).all()
                by_sid = {r[0]: r[1] for r in rows}
                assert by_sid[sid_a] == tid_a
                assert by_sid[sid_b] == tid_b
                assert by_sid["task:no-conversation"] is None

                ev_rows = (
                    await conn.execute(
                        sa.text(
                            "SELECT session_id, tenant_id FROM agent_event_logs"
                        )
                    )
                ).all()
                ev_by_sid = {r[0]: r[1] for r in ev_rows}
                assert ev_by_sid[sid_a] == tid_a
                assert ev_by_sid[sid_b] == tid_b
        finally:
            await engine.dispose()