"""B7 验收闸门 — 混合恢复必须接进主链路，event_log 不得生产零消费。

审计证据：save/load_checkpoint_with_event_log 仅测试调用；agent_event_logs
恒空（chat 请求从未进入 event_log_scope，trace_node 拾取不到 manager）；
delivery_chain.py:226 的消费方查空表。

接线三处（缺一不可）：
1. chat_service.stream_chat 的 engine.answer 迭代包 event_log_scope →
   trace_node 真实写事件（G1/G2）；
2. save_session 走 save_checkpoint_with_event_log → 快照记录 base_seq（G3）；
3. build_context 的 checkpoint 加载走 load_checkpoint_with_event_log →
   重放 base_seq 之后的增量（G4），单租户 NULL 行可见（G5）。

基座：C1 rls_harness（真实 RLS + 非 superuser 运行角色）。
chat 编排用 test_chat_disconnect 同款替身边界（引擎/权限/意图/收尾），
事件写入与 checkpoint 存取全部走真实 DB。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
def _isolated_session_uses_app_role(rls_db, monkeypatch):
    """_load_checkpoint_isolated 走 async_session_factory（模块级全局，
    默认绑 superuser DSN 会绕过 RLS）。改绑运行角色工厂，让加载路径
    打到与生产一致的 fail-closed 边界。"""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import app.database as database

    engine = create_async_engine(rls_db.app_url, poolclass=NullPool)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", factory)
    yield
    # engine 随用例结束释放（NullPool 无长连接）


@pytest.fixture(autouse=True)
def _stub_llm_boundaries(monkeypatch):
    """LLM/向量外部边界打桩（B1 同款）；记忆 DB 读写全部真实。"""
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


async def _seed_conversation(rls_db, tenant_id: str | None):
    """owner 身份种用户 + 会话（superuser 只用于准备数据）。"""
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
                email=f"b7-{uuid.uuid4()}@ekb.local",
                hashed_password="x",
                name="B7",
            )
            s.add(user)
            await s.flush()
            conv = Conversation(
                user_id=user.id,
                tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
            )
            s.add(conv)
            await s.commit()
            return user.id, conv.id
    finally:
        await engine.dispose()


async def _owner_rows(rls_db, sql: str, *args):
    import asyncpg

    conn = await asyncpg.connect(rls_db.owner_url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def _make_chat_service(session, user, tenant_id, conversation_id):
    """组装被测 ChatService — 引擎/权限/收尾为替身，DB 会话真实。

    替身引擎在 answer() 内模拟 trace_node 的 contextvar 拾取动作：
    拿到激活的 EventLogManager 就追加一条 node_end 事件。chat 主链路
    若未进入 event_log_scope，拾取结果为 None，事件恒不写（审计现状）。
    """
    from app.services.chat_service import ChatService, PreparedChat
    from app.utils.sse import SSEEvent, SSEEventType

    class _FakeEngine:
        async def answer(self, **kwargs):
            from app.memory.event_log import get_current_event_log

            manager = get_current_event_log()
            if manager is not None:
                await manager.append(
                    session_id=kwargs["session_id"],
                    event_type="node_end",
                    node_name="think",
                    output_data={"iteration": 1},
                    iteration=1,
                )
            yield "答案"
            yield SSEEvent(data={}, event=SSEEventType.DONE)

    with (
        patch("app.services.chat_service.get_llm_provider"),
        patch("app.services.chat_service.MemoryManager") as mm_cls,
        patch("app.services.chat_service.ConversationRepository"),
        patch("app.services.chat_service.MessageRepository"),
    ):
        svc = ChatService(session, user, tenant_id=tenant_id)

    mm = mm_cls.return_value
    mm.build_context = AsyncMock(return_value=MagicMock())
    mm.save_session = AsyncMock()
    svc.memory = mm

    msg_repo = MagicMock()
    msg_repo.create_message = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    svc.msg_repo = msg_repo

    async def _fake_prepare(**kwargs):
        return PreparedChat(
            query=kwargs["query"],
            conversation_id=conversation_id,
            agent_type="qa",
            tenant_id=tenant_id,
            memory_context="ctx",
            resolved_model_id="default",
            default_model_id="default",
        )

    svc.prepare_chat = _fake_prepare
    return svc, _FakeEngine()


def _stream_env_patches(fake_engine):
    perm = MagicMock()
    perm.get_accessible_kb_ids = AsyncMock(return_value={"kb-1"})
    perm.allowed_classifications = MagicMock(return_value=["public"])
    fake_settings = MagicMock()
    fake_settings.INTENT_ROUTER_ENABLED = False
    fake_settings.CONTRADICTION_DETECTION_ENABLED = False
    return (
        patch("app.services.chat_service.get_rag_engine", return_value=fake_engine),
        patch("app.services.permission_service.PermissionService", return_value=perm),
        patch("app.config.get_settings", return_value=fake_settings),
    )


class TestB7ChatEventScope:
    async def test_chat_stream_writes_node_events(self, rls_db):
        """多租户 chat 一轮 → agent_event_logs 必须有本租户节点事件。"""
        from app.core.tenant_context import tenant_scope

        user_id, conv_id = await _seed_conversation(rls_db, TENANT_A)
        user = SimpleNamespace(id=user_id, role="editor", clearance_level="internal")

        async with rls_db.factory() as session:
            async with tenant_scope(session, TENANT_A):
                svc, engine = _make_chat_service(session, user, TENANT_A, conv_id)
                p1, p2, p3 = _stream_env_patches(engine)
                with (
                    patch.object(svc, "_persist_assistant_result", new=AsyncMock()),
                    p1,
                    p2,
                    p3,
                ):
                    chunks = [
                        c
                        async for c in svc.chat(
                            query="混合恢复提问",
                            conversation_id=conv_id,
                            agent_type="qa",
                        )
                    ]
                assert chunks, "流必须正常走完（正向证人）"
                await session.commit()

        rows = await _owner_rows(
            rls_db,
            "SELECT tenant_id, node_name FROM agent_event_logs "
            "WHERE session_id = $1",
            str(conv_id),
        )
        assert len(rows) == 1, "chat 一轮必须落一条节点事件（审计现状：恒空）"
        assert str(rows[0]["tenant_id"]) == TENANT_A
        assert rows[0]["node_name"] == "think"

    async def test_single_tenant_null_chat_stream_writes_events(self, rls_db):
        """单租户 NULL 会话：无 GUC 请求也必须落事件（B3 同款审计 '*' 通道）。"""
        user_id, conv_id = await _seed_conversation(rls_db, None)
        user = SimpleNamespace(id=user_id, role="editor", clearance_level="internal")

        async with rls_db.factory() as session:
            # 单租户请求无租户 GUC（middleware 只认 JWT 租户）
            svc, engine = _make_chat_service(session, user, None, conv_id)
            p1, p2, p3 = _stream_env_patches(engine)
            with (
                patch.object(svc, "_persist_assistant_result", new=AsyncMock()),
                p1,
                p2,
                p3,
            ):
                chunks = [
                    c
                    async for c in svc.chat(
                        query="单租户提问",
                        conversation_id=conv_id,
                        agent_type="qa",
                    )
                ]
            assert chunks
            await session.commit()

        rows = await _owner_rows(
            rls_db,
            "SELECT tenant_id FROM agent_event_logs WHERE session_id = $1",
            str(conv_id),
        )
        assert len(rows) == 1, "单租户 NULL 会话事件不得被 fail-closed 静默拒绝"
        assert rows[0]["tenant_id"] is None


class TestB7HybridSaveLoad:
    async def test_save_session_records_base_seq(self, rls_db):
        """save_session 的快照必须记录对应 EventLog base_seq。"""
        from app.core.tenant_context import tenant_scope
        from app.memory.event_log import EventLogManager
        from app.memory.memory_manager import MemoryManager

        user_id, conv_id = await _seed_conversation(rls_db, TENANT_A)
        sid = str(conv_id)
        async with rls_db.factory() as session:
            async with tenant_scope(session, TENANT_A):
                mgr = EventLogManager(session)
                assert await mgr.append(sid, "node_end", "think", iteration=1) == 1
                assert await mgr.append(sid, "node_end", "retrieve", iteration=1) == 2
                memory = MemoryManager(session, tenant_id=uuid.UUID(TENANT_A))
                await memory.save_session(
                    user_id=user_id,
                    session_id=sid,
                    agent_state={"iteration": 2, "retrieved_docs": ["d1"]},
                )
            await session.commit()

        rows = await _owner_rows(
            rls_db,
            "SELECT agent_state->>'_base_seq' AS base_seq "
            "FROM agent_checkpoints WHERE session_id = $1",
            sid,
        )
        assert len(rows) == 1
        assert (
            rows[0]["base_seq"] == "2"
        ), "快照必须锚定到最新事件 seq（审计现状：无 _base_seq）"

    async def test_build_context_replays_events_after_checkpoint(self, rls_db):
        """加载 = 快照 + base_seq 之后事件重放（增量恢复真实生效）。"""
        from app.core.tenant_context import tenant_scope
        from app.memory.checkpoint import CheckpointManager
        from app.memory.event_log import EventLogManager
        from app.memory.memory_manager import MemoryManager

        user_id, conv_id = await _seed_conversation(rls_db, TENANT_A)
        sid = str(conv_id)
        async with rls_db.factory() as session:
            async with tenant_scope(session, TENANT_A):
                cp = CheckpointManager(session, tenant_id=uuid.UUID(TENANT_A))
                await cp.save_checkpoint(
                    sid,
                    {"iteration": 1, "retrieved_docs": ["d1"], "_base_seq": 1},
                    1,
                )
                mgr = EventLogManager(session)
                # seq 1：快照锚点对应的已重放事件
                assert (
                    await mgr.append(
                        sid,
                        "node_end",
                        "think",
                        output_data={"iteration": 1},
                        iteration=1,
                    )
                    == 1
                )
                # seq 2：快照之后的增量事件 — 检索又命中一篇文档
                assert (
                    await mgr.append(
                        sid,
                        "node_end",
                        "retrieve",
                        output_data={"retrieved_docs": ["d2"]},
                        iteration=2,
                    )
                    == 2
                )
            # checkpoint 分支走独立连接，种子必须先提交才可见
            await session.commit()
            async with tenant_scope(session, TENANT_A):
                memory = MemoryManager(session, tenant_id=uuid.UUID(TENANT_A))
                ctx = await memory.build_context(user_id=user_id, session_id=sid)
            await session.commit()

        assert ctx.checkpoint is not None
        assert ctx.checkpoint["retrieved_docs"] == [
            "d1",
            "d2",
        ], "base_seq 之后的事件增量必须重放进状态（审计现状：只读快照不重放）"

    async def test_single_tenant_checkpoint_visible_without_guc(self, rls_db):
        """单租户 NULL checkpoint：无 GUC 的 build_context 必须读得到。"""
        from app.core.tenant_context import system_scope
        from app.memory.checkpoint import CheckpointManager
        from app.memory.memory_manager import MemoryManager

        user_id, conv_id = await _seed_conversation(rls_db, None)
        sid = str(conv_id)
        async with rls_db.factory() as session:
            async with system_scope(session, reason="b7_gate.seed_null_checkpoint"):
                await CheckpointManager(session).save_checkpoint(
                    sid, {"iteration": 4, "retrieved_docs": ["d1", "d2"]}, 4
                )
            await session.commit()

        async with rls_db.factory() as session:
            # 单租户请求会话无 GUC —— 修复前 fail-closed 恒读空
            memory = MemoryManager(session)
            ctx = await memory.build_context(user_id=user_id, session_id=sid)

        assert ctx.checkpoint is not None, "单租户 NULL checkpoint 不得对加载路径隐形"
        assert ctx.checkpoint["iteration"] == 4
