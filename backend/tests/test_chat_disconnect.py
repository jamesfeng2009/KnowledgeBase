"""P1 验收 — Chat SSE 断线语义：断线不得留下残缺 assistant 消息。

场景对应文章验收："客服中途关页 → SSE 生成器被取消（Starlette 在客户端
断开时取消响应任务）→ 生成停止"。本测试在单元边界用 aclose() 模拟同一
取消语义：消费两个 token 后关闭 stream_chat 生成器。

验收断言：
    1. 中断后消息仓库只有用户消息 — 绝无残缺 assistant 回复；
    2. _persist_assistant_result 未被执行（它只在流正常走完后到达）；
    3. 对照组：流正常走完时 assistant 消息完整落库 —
       证明断言本身能区分"断线"与"完成"两种结局，不是恒真。

前端约定（Astro 仓库，后端不加端点）：会话消息列表最后一条是 user 且
其后无 assistant 时显示"重新生成"，点击用同一 conversation_id 重发
（RAG 缓存 + 重复提问检测兜底重放成本）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.chat_service import ChatService, PreparedChat
from app.utils.sse import SSEEvent, SSEEventType


def _fake_engine(chunks: list):
    """构造引擎替身 — answer() 按给定序列 yield token / 事件。"""

    class _FakeEngine:
        async def answer(self, **kwargs):
            for c in chunks:
                yield c

    return _FakeEngine()


def _make_service(chunks: list) -> ChatService:
    """组装被测 ChatService — DB/仓库/记忆全部替身，仅流式编排为真实现。"""
    cid = uuid4()
    db = MagicMock()
    db.commit = AsyncMock()
    db.close = AsyncMock()
    db.execute = AsyncMock()

    with (
        patch("app.services.chat_service.get_llm_provider"),
        patch("app.services.chat_service.MemoryManager") as mm_cls,
        patch("app.services.chat_service.ConversationRepository"),
        patch("app.services.chat_service.MessageRepository"),
    ):
        svc = ChatService(db, SimpleNamespace(id=uuid4(), role="editor"))

    mm = mm_cls.return_value
    mm.build_context = AsyncMock(return_value=MagicMock())
    mm.save_session = AsyncMock()
    mm.extract_and_save_facts = AsyncMock()
    mm.extract_and_save_key_decisions = AsyncMock()
    svc.memory = mm

    calls: list[tuple] = []
    msg_repo = MagicMock()

    async def _create_message(conversation_id, role, content, **kwargs):
        calls.append((str(conversation_id), role, content))
        return SimpleNamespace(id=uuid4())

    msg_repo.create_message = _create_message
    svc.msg_repo = msg_repo
    svc._calls = calls
    svc._cid = cid

    # prepare_chat 替身 — 与真实现同契约：持久化用户消息后返回上下文
    async def _fake_prepare(**kwargs):
        await msg_repo.create_message(cid, "user", kwargs["query"])
        return PreparedChat(
            query=kwargs["query"],
            conversation_id=cid,
            agent_type="qa",
            tenant_id=None,
            memory_context="ctx",
            resolved_model_id="default",
            default_model_id="default",
        )

    svc.prepare_chat = _fake_prepare
    return svc


def _patch_stream_env(fake_engine):
    """屏蔽 stream_chat 的外围依赖：引擎选择 / 权限 / 意图 / 矛盾检测。"""
    perm = MagicMock()
    perm.get_accessible_kb_ids = AsyncMock(return_value={"kb-1"})
    perm.allowed_classifications = MagicMock(return_value=["public"])
    fake_settings = MagicMock()
    fake_settings.INTENT_ROUTER_ENABLED = False
    fake_settings.CONTRADICTION_DETECTION_ENABLED = False
    return (
        patch("app.services.chat_service.get_rag_engine", return_value=fake_engine),
        patch(
            "app.services.permission_service.PermissionService",
            return_value=perm,
        ),
        patch("app.config.get_settings", return_value=fake_settings),
    )


class TestChatDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_leaves_no_partial_assistant_message(self) -> None:
        """断线：消费两个 token 后取消 → 无 assistant 消息、收尾未执行。"""
        engine = _fake_engine(["部分一", "部分二", "残缺尾巴"])
        svc = _make_service(engine)
        p1, p2, p3 = _patch_stream_env(engine)

        with patch.object(
            ChatService, "_persist_assistant_result", new=AsyncMock()
        ) as spy_persist, p1, p2, p3:
            gen = svc.chat(query="测试问题", conversation_id=None, agent_type="qa")
            await gen.__anext__()  # META 事件
            assert await gen.__anext__() == "部分一"
            assert await gen.__anext__() == "部分二"
            # 客户端此刻关页 — Starlette 取消响应任务，等价于 aclose
            await gen.aclose()

        spy_persist.assert_not_awaited(), "断线后收尾持久化绝不能执行"
        roles = [role for _, role, _ in svc._calls]
        assert roles == ["user"], f"只应有用户消息，实际：{svc._calls}"
        assert all("残缺" not in c for _, _, c in svc._calls)

    @pytest.mark.asyncio
    async def test_full_stream_persists_complete_assistant_message(self) -> None:
        """对照组：流正常走完 → assistant 消息完整落库（断言区分两种结局）。"""
        engine = _fake_engine(["答案A", "答案B", SSEEvent(data={}, event=SSEEventType.DONE)])
        svc = _make_service(engine)
        p1, p2, p3 = _patch_stream_env(engine)

        with p1, p2, p3:
            chunks = [
                c
                async for c in svc.chat(
                    query="测试问题", conversation_id=None, agent_type="qa"
                )
            ]

        roles = [role for _, role, _ in svc._calls]
        assert roles == ["user", "assistant"], f"两方消息都应落库：{svc._calls}"
        assistant = [c for _, r, c in svc._calls if r == "assistant"][0]
        assert assistant == "答案A答案B", "落库的是完整拼接回复，非残缺片段"
        assert any(
            isinstance(c, SSEEvent) and c.event == SSEEventType.DONE for c in chunks
        )
