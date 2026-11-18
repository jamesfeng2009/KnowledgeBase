"""B4 验收闸门 — chat 路径不得用存根覆写 agent checkpoint；L2 提示不得谎称可恢复。

审计证据：
  - chat_service._persist_assistant_result 同步降级写
    agent_state={"iteration": 0, "retrieved_docs": []}，每轮覆写
    agents/base.py 落下的真实状态；
  - memory_tasks 同款存根默认值（agent_state or {...}）；
  - MemoryContext.to_system_prompt 把 checkpoint 渲染成
    「从上次中断处恢复」，而加载路径只渲染文本、从不恢复状态。

修复契约：
  1. chat 同步降级 / 异步任务不带 agent_state 时，save_session 跳过
     checkpoint 写入（干净跳过，不是吞异常）；
  2. 带真实 agent_state 的路径（Agent Loop）照旧写入；
  3. prompt 文本改为诚实的「历史进度参考」。

基座：conftest db_session（真实 PostgreSQL；B4 不涉及 RLS 断言）。
LLM 提取/embedder 属外部边界，按 B1/B3 同款方式打桩。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

REAL_STATE = {
    "iteration": 3,
    "retrieved_docs": ["d1", "d2"],
    "tool_results": {"search": ["r1"]},
}


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
    monkeypatch.setattr(
        MemoryManager, "extract_and_save_key_decisions", AsyncMock(return_value=None)
    )


async def _seed_checkpoint(db, session_id: str) -> None:
    from app.memory.checkpoint import CheckpointManager

    await CheckpointManager(db).save_checkpoint(
        session_id, dict(REAL_STATE), REAL_STATE["iteration"]
    )
    await db.commit()


async def _load_state(db, session_id: str):
    from app.memory.checkpoint import CheckpointManager

    return await CheckpointManager(db).load_checkpoint(session_id)


async def _seed_user_and_conversation(db):
    from app.models.conversation import Conversation
    from app.models.user import User

    user = User(
        email=f"b4-{uuid.uuid4()}@ekb.local", hashed_password="x", name="B4"
    )
    db.add(user)
    await db.flush()
    conv = Conversation(user_id=user.id)
    db.add(conv)
    await db.commit()
    return user.id, str(conv.id)


class TestB4ChatPathNoClobber:
    async def test_chat_sync_fallback_preserves_agent_checkpoint(
        self, db_session, monkeypatch
    ):
        """主闸门：chat 同步降级一轮后，Agent Loop 的真实 checkpoint 不被覆写。"""
        from app.memory.memory_manager import MemoryManager
        from app.services.chat_service import ChatService

        user_id, session_id = await _seed_user_and_conversation(db_session)
        await _seed_checkpoint(db_session, session_id)

        monkeypatch.setattr(
            "tasks.memory_tasks.dispatch_memory_write", lambda **kw: False
        )
        import app.memory.short_term_cache as stc

        monkeypatch.setattr(stc.short_term_cache, "append", AsyncMock())

        svc = ChatService.__new__(ChatService)
        svc.db = db_session
        svc.user = SimpleNamespace(id=user_id)
        svc._tenant_id = None
        svc.msg_repo = AsyncMock()
        svc.memory = MemoryManager(db_session)

        await svc._persist_assistant_result(
            uuid.UUID(session_id), "用户提问", "助手回答", "model-x"
        )

        loaded = await _load_state(db_session, session_id)
        assert loaded is not None
        assert loaded["iteration"] == 3
        assert loaded["retrieved_docs"] == ["d1", "d2"]

    async def test_save_session_none_skips_checkpoint_cleanly(
        self, db_session, monkeypatch
    ):
        """save_session(agent_state=None) 必须干净跳过 checkpoint，
        而不是抛 AttributeError 再被吞掉；摘要照常落库（正向证人）。"""
        import app.memory.memory_manager as mm
        from app.models.memory import MemoryFact
        from sqlalchemy import select

        user_id, session_id = await _seed_user_and_conversation(db_session)
        await _seed_checkpoint(db_session, session_id)

        error_spy = MagicMock()
        monkeypatch.setattr(mm.logger, "error", error_spy)

        mgr = mm.MemoryManager(db_session)
        await mgr.save_session(
            user_id=user_id,
            session_id=session_id,
            agent_state=None,
            summary="B4 摘要证人",
        )
        await db_session.commit()

        error_spy.assert_not_called()
        loaded = await _load_state(db_session, session_id)
        assert loaded["iteration"] == 3

        facts = (
            await db_session.execute(
                select(MemoryFact).where(
                    MemoryFact.fact_text == "B4 摘要证人",
                    MemoryFact.category == "summary",
                )
            )
        ).scalars().all()
        assert len(facts) == 1

    async def test_memory_task_without_state_preserves_checkpoint(
        self, db_session
    ):
        """异步任务不带 agent_state（chat 派发路径）不得覆写 checkpoint。"""
        from tasks.memory_tasks import _persist_memory_async

        user_id, session_id = await _seed_user_and_conversation(db_session)
        await _seed_checkpoint(db_session, session_id)

        result = await _persist_memory_async(
            user_id=str(user_id),
            session_id=session_id,
            query="异步提问",
            summary="异步摘要",
        )
        assert result["session_saved"] is True
        loaded = await _load_state(db_session, session_id)
        assert loaded["iteration"] == 3
        assert loaded["retrieved_docs"] == ["d1", "d2"]

    async def test_memory_task_with_real_state_still_writes(self, db_session):
        """正向证人：Agent Loop 带真实状态调用时，checkpoint 照旧更新。"""
        from tasks.memory_tasks import _persist_memory_async

        user_id, session_id = await _seed_user_and_conversation(db_session)
        await _seed_checkpoint(db_session, session_id)

        new_state = {"iteration": 5, "retrieved_docs": ["d1", "d2", "d3"]}
        result = await _persist_memory_async(
            user_id=str(user_id),
            session_id=session_id,
            query="agent 提问",
            agent_state=new_state,
        )
        assert result["session_saved"] is True
        loaded = await _load_state(db_session, session_id)
        assert loaded["iteration"] == 5


class TestB4PromptHonesty:
    def test_progress_reference_not_claimed_as_resume(self):
        """L2 提示不得谎称「从上次中断处恢复」；进度数字仍要渲染（正向证人）。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.checkpoint = {"iteration": 3, "retrieved_docs": ["d1", "d2"]}
        prompt = ctx.to_system_prompt()
        assert "从上次中断处恢复" not in prompt
        assert "历史进度参考" in prompt
        assert "已迭代 3 次" in prompt
        assert "已检索 2 条文档" in prompt

    def test_stub_checkpoint_renders_nothing(self):
        """存根 checkpoint（iteration=0 且无检索）不渲染任何 L2 文本。"""
        from app.memory.memory_manager import MemoryContext

        ctx = MemoryContext()
        ctx.checkpoint = {"iteration": 0, "retrieved_docs": []}
        assert ctx.to_system_prompt() == ""