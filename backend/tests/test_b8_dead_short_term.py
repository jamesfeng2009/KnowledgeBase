"""B8 验收闸门 — build_context 的 recent_messages/short_term 死代码必须删除，
L1 短期窗口职责明确归 chat 的 Redis/PG 热层。

审计证据：build_context 接收 recent_messages 却从不消费，ctx.short_term
恒空；两个调用方（chat_service / agents.base）都只传当前一条 query；
SHORT_TERM_WINDOW_SIZE 死常量；真实历史靠 chat_service 的 Redis 兜底
（含 P3-C 滚动摘要压缩）。若接线填 short_term 反而会让 chat 跳过历史
兜底丢历史 —— 决策：删死代码，划清职责（用户已确认）。

红基线 = 删除契约（参数/属性/常量仍在）；正向证人 = L2/L3/L4 组装与
chat 历史注入行为不回归。
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.rls_harness import rls_db  # noqa: F401

TENANT_A = "aaaaaaaa-0000-4000-8000-00000000000a"


class TestB8DeadCodeRemoved:
    def test_dead_short_term_surface_removed(self):
        """删除契约：死参数/死属性/死常量必须全部消失。"""
        from app.memory import memory_manager as mm
        from app.memory.memory_manager import MemoryContext, MemoryManager

        assert (
            "recent_messages"
            not in inspect.signature(MemoryManager.build_context).parameters
        ), "build_context 的 recent_messages 从未被消费，必须删除"
        assert not hasattr(
            MemoryContext(), "short_term"
        ), "ctx.short_term 恒空且无填充方，属僵尸字段"
        assert (
            "render_short_term"
            not in inspect.signature(MemoryContext.to_system_prompt).parameters
        ), "render_short_term 渲染的 short_term 恒空，属死参数"
        assert not hasattr(mm, "SHORT_TERM_WINDOW_SIZE"), "死常量必须删除"


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
def _isolated_session_uses_rls_db(monkeypatch, rls_db):
    """_load_checkpoint_isolated 走生产全局 async_session_factory，
    必须绑到 RLS 测试库（app 角色），否则读的是 dev 库恒 None。"""
    import app.database as database

    monkeypatch.setattr(database, "async_session_factory", rls_db.factory)


@pytest.fixture(autouse=True)
def _stub_llm_boundaries(monkeypatch):
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


class TestB8LivingLayersPreserved:
    async def test_build_context_still_assembles_l2_l3_l4(self, rls_db):
        """正向证人：删除死代码后 L2/L3/L4 组装与渲染不回归。"""
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from app.core.tenant_context import tenant_scope
        from app.memory.checkpoint import CheckpointManager
        from app.memory.mem0_manager import Mem0Manager
        from app.memory.memory_manager import MemoryManager
        from app.models.conversation import Conversation
        from app.models.user import User

        url = rls_db.owner_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(url)
        try:
            factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as s:
                user = User(
                    email=f"b8-{uuid.uuid4()}@ekb.local",
                    hashed_password="x",
                    name="B8",
                )
                s.add(user)
                await s.flush()
                conv = Conversation(user_id=user.id, tenant_id=uuid.UUID(TENANT_A))
                s.add(conv)
                await s.commit()
                user_id, conv_id = user.id, conv.id
        finally:
            await engine.dispose()

        sid = str(conv_id)
        async with rls_db.factory() as session:
            async with tenant_scope(session, TENANT_A):
                await CheckpointManager(
                    session, tenant_id=uuid.UUID(TENANT_A)
                ).save_checkpoint(sid, {"iteration": 2, "retrieved_docs": ["d1"]}, 2)
                await Mem0Manager(session, tenant_id=uuid.UUID(TENANT_A)).add_fact(
                    user_id=user_id,
                    fact_text="answer_style: 简洁",
                    category="preference",
                    fact_key="answer_style",
                    fact_value="简洁",
                )
            # checkpoint 分支走独立连接，种子必须先提交才可见
            await session.commit()
            async with tenant_scope(session, TENANT_A):
                memory = MemoryManager(session, tenant_id=uuid.UUID(TENANT_A))
                ctx = await memory.build_context(user_id=user_id, session_id=sid)
            await session.commit()

        assert ctx.checkpoint is not None and ctx.checkpoint["iteration"] == 2
        assert any(f["category"] == "preference" for f in ctx.user_facts)
        prompt = ctx.to_system_prompt()
        assert "answer_style: 简洁" in prompt
        assert "历史进度参考" in prompt

    async def test_chat_history_injection_still_works(self):
        """正向证人：L1 历史仍由 chat 的 Redis/PG 热层注入（职责划清后不丢历史）。"""
        from app.memory.memory_manager import MemoryContext
        from app.services.chat_service import ChatService

        svc = ChatService.__new__(ChatService)
        svc.llm = MagicMock()
        svc.user = SimpleNamespace(id=uuid.uuid4())
        msg_repo = MagicMock()
        msg_repo.get_by_conversation = AsyncMock(
            return_value=[
                SimpleNamespace(role="user", content="上一轮问题"),
                SimpleNamespace(role="assistant", content="上一轮回答"),
                SimpleNamespace(role="user", content="当前问题"),
            ]
        )
        svc.msg_repo = msg_repo

        fake_settings = MagicMock()
        fake_settings.CONVERSATION_SUMMARIZER_ENABLED = False
        fake_settings.CONTEXT_SELECTOR_ENABLED = False
        fake_settings.DETAIL_RECALL_ENABLED = False

        ctx = MemoryContext()
        ctx.user_facts = [{"category": "preference", "fact_text": "answer_style: 简洁"}]
        with (
            patch(
                "app.memory.short_term_cache.short_term_cache.get_window",
                new=AsyncMock(return_value=None),
            ),
            patch("app.config.get_settings", return_value=fake_settings),
        ):
            text = await svc._build_engine_memory_context(
                conversation_id=uuid.uuid4(),
                agent_type="qa",
                memory_ctx=ctx,
                resolved_query="当前问题",
            )

        assert (
            "对话历史：" in text
        ), "Redis miss 必须回填 PG 历史（L1 职责在 chat 热层）"
        assert "[用户] 上一轮问题" in text
        assert "[助手] 上一轮回答" in text
        assert (
            "当前问题" not in text.split("对话历史：")[1]
        ), "刚保存的当前消息不得重复注入"
        assert "answer_style: 简洁" in text
