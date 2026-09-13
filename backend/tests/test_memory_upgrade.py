"""P0-P2 记忆改进验收 — 对标竞品上下文记忆架构。

覆盖：
- P0  记忆写入异步化：dispatch_memory_write 派发/开关关闭/broker 不可用降级
- P1a build_context L2/L3/L4 并行加载（asyncio.gather）+ 单路失败隔离
- P1b 删除会话清理会话级记忆（working/summary 事实停用 + Checkpoint/
      EventLog 删除 + L1 热层失效；preference 保留）
- P2a L1 短期窗口 Redis 热层（append/get_window/replace/invalidate + 降级）
- P2b 事实提取 prompt 显式负规则（寒暄/知识库内容/工具结果不写入）
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.memory.memory_manager import MemoryManager


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


class _SelectiveMem0:
    """模拟 Mem0Manager.search_facts — 可指定某类别抛错（单路失败隔离）。"""

    def __init__(
        self,
        facts: list | None = None,
        working: list | None = None,
        fail_category: str | None = None,
    ):
        self.facts = facts or []
        self.working = working or []
        self.fail_category = fail_category
        self.calls: list[str | None] = []

    async def search_facts(
        self, user_id, query=None, category=None, limit=10, **kwargs
    ):
        self.calls.append(category)
        if self.fail_category is not None and category == self.fail_category:
            raise RuntimeError("mem0 down")
        if category == "working":
            return list(self.working)
        return list(self.facts)


def _make_manager(fail_category: str | None = None) -> tuple[MemoryManager, _SelectiveMem0]:
    mgr = MemoryManager(db=MagicMock(), sidecar=None)
    mem0 = _SelectiveMem0(
        facts=[SimpleNamespace(id=uuid.uuid4(), category="preference", fact_text="偏好A",
                                fact_key="", fact_value="")],
        working=[SimpleNamespace(id=uuid.uuid4(), category="working", fact_text="工作B",
                                  fact_key="", fact_value="")],
        fail_category=fail_category,
    )
    mgr.mem0 = mem0
    return mgr, mem0


# ----------------------------------------------------------------------
# P1a build_context 并行加载
# ----------------------------------------------------------------------


class TestP1aParallelBuildContext:
    """L2/L3/L4 并行加载：无 session_id 不报错 + 单路失败隔离。"""

    @pytest.mark.asyncio
    async def test_no_session_id_ok(self):
        """回归：session_id=None 时不向 asyncio.gather 传 None（原实现会 TypeError）。"""
        mgr, mem0 = _make_manager()
        ctx = await mgr.build_context(user_id=uuid.uuid4(), query="问题")
        assert ctx.checkpoint is None
        # build_context 统一转 dict（消费端均为 dict 式访问）
        assert ctx.user_facts[0]["fact_text"] == "偏好A"
        assert ctx.working_memory[0]["fact_text"] == "工作B"
        assert mem0.calls == [None, "working"]  # L3 + L4 各一次

    @pytest.mark.asyncio
    async def test_single_path_failure_isolated(self):
        """L3 抛错只影响 user_facts，L4 照常返回，整体不抛异常。"""
        mgr, mem0 = _make_manager(fail_category=None)
        # L3 调用是 category=None 的那次 — 用计数器让第一次调用抛错
        calls = {"n": 0}
        original = mem0.search_facts

        async def flaky(user_id, query=None, category=None, limit=10, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("mem0 down")
            return await original(user_id, query=query, category=category,
                                  limit=limit, **kw)

        mem0.search_facts = flaky
        ctx = await mgr.build_context(user_id=uuid.uuid4(), query="问题")
        assert ctx.user_facts == []          # L3 失败 → 空列表
        assert len(ctx.working_memory) == 1  # L4 不受影响

    @pytest.mark.asyncio
    async def test_checkpoint_parallel_load(self):
        """session_id 存在时 Checkpoint 与 L3/L4 并行加载，结果正常聚合。"""
        mgr, _mem0 = _make_manager()
        mgr.checkpoint = MagicMock(
            load_checkpoint=AsyncMock(
                return_value={"iteration": 2, "retrieved_docs": ["d1"]}
            )
        )
        ctx = await mgr.build_context(
            user_id=uuid.uuid4(), session_id="sess-1", query="问题"
        )
        assert ctx.checkpoint is not None
        assert ctx.checkpoint["iteration"] == 2
        assert len(ctx.user_facts) == 1


# ----------------------------------------------------------------------
# P1b 删除会话清理记忆
# ----------------------------------------------------------------------


class TestP1bCleanupConversationMemory:
    """cleanup_conversation_memory 编排：事实停用 + Checkpoint/EventLog + 热层。"""

    def _prepare(self, monkeypatch: pytest.MonkeyPatch, mgr: MemoryManager):
        msg_ids = [uuid.uuid4(), uuid.uuid4()]
        fake_repo = MagicMock()
        fake_repo.get_by_conversation = AsyncMock(
            return_value=[SimpleNamespace(id=m) for m in msg_ids]
        )
        monkeypatch.setattr(
            "app.repositories.conversation_repository.MessageRepository",
            MagicMock(return_value=fake_repo),
        )
        fake_elm = MagicMock(delete_all=AsyncMock(return_value=5))
        monkeypatch.setattr(
            "app.memory.event_log.EventLogManager",
            MagicMock(return_value=fake_elm),
        )
        fake_cache = MagicMock(invalidate=AsyncMock(return_value=True))
        monkeypatch.setattr(
            "app.memory.short_term_cache.short_term_cache", fake_cache
        )
        return msg_ids, fake_elm, fake_cache

    @pytest.mark.asyncio
    async def test_cleanup_orchestrates_all_paths(self, monkeypatch):
        user_id, conv_id = uuid.uuid4(), uuid.uuid4()
        mgr, _mem0 = _make_manager()
        mgr.mem0 = MagicMock(
            deactivate_facts_by_source_refs=AsyncMock(return_value=3)
        )
        mgr.checkpoint = MagicMock(delete_checkpoint=AsyncMock())
        msg_ids, fake_elm, fake_cache = self._prepare(monkeypatch, mgr)

        stats = await mgr.cleanup_conversation_memory(
            user_id=user_id, session_id=str(conv_id), conversation_id=conv_id
        )

        assert stats == {"facts": 3, "checkpoints": 1, "events": 5}
        mgr.mem0.deactivate_facts_by_source_refs.assert_awaited_once_with(
            user_id, msg_ids, categories=["working", "summary"]
        )
        mgr.checkpoint.delete_checkpoint.assert_awaited_once_with(str(conv_id))
        fake_elm.delete_all.assert_awaited_once_with(str(conv_id))
        fake_cache.invalidate.assert_awaited_once_with(str(conv_id))

    @pytest.mark.asyncio
    async def test_cleanup_single_path_failure_continues(self, monkeypatch):
        """事实停用抛错不阻断 Checkpoint/EventLog/热层清理。"""
        user_id, conv_id = uuid.uuid4(), uuid.uuid4()
        mgr, _mem0 = _make_manager()
        mgr.mem0 = MagicMock(
            deactivate_facts_by_source_refs=AsyncMock(
                side_effect=RuntimeError("db down")
            )
        )
        mgr.checkpoint = MagicMock(delete_checkpoint=AsyncMock())
        _msg_ids, fake_elm, fake_cache = self._prepare(monkeypatch, mgr)

        stats = await mgr.cleanup_conversation_memory(
            user_id=user_id, session_id=str(conv_id), conversation_id=conv_id
        )

        assert stats["facts"] == 0
        assert stats["checkpoints"] == 1
        assert stats["events"] == 5
        fake_cache.invalidate.assert_awaited_once()


class TestP1bDeleteConversationService:
    """ChatService.delete_conversation：归属校验 + 软删除 + 记忆清理。"""

    def _make_service(self, user_id: uuid.UUID):
        from app.services.chat_service import ChatService

        svc = ChatService.__new__(ChatService)
        svc.db = MagicMock()
        svc.db.commit = AsyncMock()
        svc.user = SimpleNamespace(id=user_id)
        svc._tenant_id = None
        svc.conv_repo = MagicMock()
        svc.conv_repo.get_by_id = AsyncMock()
        svc.conv_repo.soft_delete = AsyncMock()
        svc.msg_repo = MagicMock()
        svc.memory = MagicMock()
        svc.memory.cleanup_conversation_memory = AsyncMock(
            return_value={"facts": 0, "checkpoints": 1, "events": 0}
        )
        return svc

    @pytest.mark.asyncio
    async def test_delete_success(self):
        uid, cid = uuid.uuid4(), uuid.uuid4()
        svc = self._make_service(uid)
        svc.conv_repo.get_by_id.return_value = SimpleNamespace(user_id=uid)
        svc.conv_repo.soft_delete.return_value = True

        assert await svc.delete_conversation(cid) is True
        svc.conv_repo.soft_delete.assert_awaited_once_with(cid)
        svc.memory.cleanup_conversation_memory.assert_awaited_once_with(
            user_id=uid, session_id=str(cid), conversation_id=cid
        )
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_not_owner_returns_false(self):
        svc = self._make_service(uuid.uuid4())
        svc.conv_repo.get_by_id.return_value = SimpleNamespace(
            user_id=uuid.uuid4()  # 他人会话
        )

        assert await svc.delete_conversation(uuid.uuid4()) is False
        svc.conv_repo.soft_delete.assert_not_awaited()
        svc.memory.cleanup_conversation_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_not_found_returns_false(self):
        svc = self._make_service(uuid.uuid4())
        svc.conv_repo.get_by_id.return_value = None

        assert await svc.delete_conversation(uuid.uuid4()) is False


class TestP1bDeleteConversationAPI:
    """DELETE /conversations/{conv_id} 路由：成功 code=0 / 不存在 code=404。"""

    @pytest.mark.asyncio
    async def test_delete_ok(self, monkeypatch):
        from app.api.v1.chat import delete_conversation as route

        fake_service = MagicMock(delete_conversation=AsyncMock(return_value=True))
        monkeypatch.setattr(
            "app.api.v1.chat.ChatService", MagicMock(return_value=fake_service)
        )
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=None))
        resp = await route(
            request,
            uuid.uuid4(),
            db=MagicMock(),
            user=SimpleNamespace(id=uuid.uuid4()),
        )
        assert resp.code == 0

    @pytest.mark.asyncio
    async def test_delete_missing_returns_404(self, monkeypatch):
        from app.api.v1.chat import delete_conversation as route

        fake_service = MagicMock(delete_conversation=AsyncMock(return_value=False))
        monkeypatch.setattr(
            "app.api.v1.chat.ChatService", MagicMock(return_value=fake_service)
        )
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=None))
        resp = await route(
            request,
            uuid.uuid4(),
            db=MagicMock(),
            user=SimpleNamespace(id=uuid.uuid4()),
        )
        assert resp.code == 404


# ----------------------------------------------------------------------
# P0 记忆写入异步化
# ----------------------------------------------------------------------


class TestP0DispatchMemoryWrite:
    """dispatch_memory_write：开关 / 派发成功 / broker 不可用降级。"""

    def test_disabled_returns_false(self, monkeypatch):
        from tasks.memory_tasks import dispatch_memory_write

        monkeypatch.setattr(
            get_settings(), "MEMORY_ASYNC_WRITE_ENABLED", False
        )
        assert (
            dispatch_memory_write(user_id="u", session_id="s", query="q") is False
        )

    def test_dispatch_success_returns_true(self, monkeypatch):
        import tasks.memory_tasks as mt

        monkeypatch.setattr(get_settings(), "MEMORY_ASYNC_WRITE_ENABLED", True)
        sent: dict = {}
        fake_task = MagicMock()
        fake_task.delay.side_effect = lambda **kw: (sent.update(kw), MagicMock())[1]
        monkeypatch.setattr(mt, "persist_conversation_memory", fake_task)

        assert (
            mt.dispatch_memory_write(
                user_id="u", session_id="s", query="q", extract_decisions=True
            )
            is True
        )
        assert sent["extract_decisions"] is True
        assert sent["session_id"] == "s"

    def test_broker_down_falls_back_to_sync(self, monkeypatch):
        import tasks.memory_tasks as mt

        monkeypatch.setattr(get_settings(), "MEMORY_ASYNC_WRITE_ENABLED", True)
        fake_task = MagicMock()
        fake_task.delay.side_effect = RuntimeError("broker down")
        monkeypatch.setattr(mt, "persist_conversation_memory", fake_task)

        assert (
            mt.dispatch_memory_write(user_id="u", session_id="s", query="q")
            is False
        )


# ----------------------------------------------------------------------
# P2a L1 短期窗口 Redis 热层
# ----------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, store: dict):
        self._store = store
        self._ops: list[tuple] = []

    # 真实 redis pipeline 支持 async with（事务）— 测试替身需对齐
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def rpush(self, key, *vals):
        self._ops.append(("rpush", key, vals))
        return self

    def ltrim(self, key, start, end):
        self._ops.append(("ltrim", key, (start, end)))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def delete(self, key):
        self._ops.append(("delete", key))
        return self

    async def execute(self):
        for op in self._ops:
            kind, key = op[0], op[1]
            arg = op[2] if len(op) > 2 else None
            if kind == "rpush":
                self._store.setdefault(key, []).extend(arg)
            elif kind == "ltrim":
                start, end = arg
                lst = self._store.get(key, [])
                n = len(lst)
                s = start if start >= 0 else max(n + start, 0)
                e = end + 1 if end >= 0 else n + end + 1
                self._store[key] = lst[s:e]
            elif kind == "delete":
                self._store.pop(key, None)
        return True


class _FakeRedis:
    """最小 Redis List 语义 — 支持 pipeline(rpush/ltrim/expire/delete)/lrange。"""

    def __init__(self):
        self.store: dict[str, list[str]] = {}

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self.store)

    async def lrange(self, key, start, end):
        lst = self.store.get(key, [])
        n = len(lst)
        s = start if start >= 0 else max(n + start, 0)
        e = end + 1 if end >= 0 else n + end + 1
        return lst[s:e]

    async def delete(self, *keys):
        count = 0
        for k in keys:
            if self.store.pop(k, None) is not None:
                count += 1
        return count


def _make_cache(**kwargs) -> "ShortTermCache":
    from app.memory.short_term_cache import ShortTermCache

    cache = ShortTermCache(redis_url="redis://faketest", **kwargs)
    cache._redis = _FakeRedis()
    cache._enabled = True
    return cache


class TestP2aShortTermCache:
    """L1 热层：写穿透 / 读取 / 窗口截断 / 失效 / 降级。"""

    @pytest.mark.asyncio
    async def test_append_and_get_window_roundtrip(self):
        cache = _make_cache()
        await cache.append("c1", "user", "问题一")
        await cache.append("c1", "assistant", "回答一")

        got = await cache.get_window("c1")
        assert got == [
            {"role": "user", "content": "问题一"},
            {"role": "assistant", "content": "回答一"},
        ]

    @pytest.mark.asyncio
    async def test_window_trim_keeps_recent(self):
        cache = _make_cache(window=4)
        for i in range(6):
            await cache.append("c2", "user", f"m{i}")

        got = await cache.get_window("c2")
        assert [m["content"] for m in got] == ["m2", "m3", "m4", "m5"]

    @pytest.mark.asyncio
    async def test_get_window_limit(self):
        cache = _make_cache()
        for i in range(5):
            await cache.append("c3", "user", f"m{i}")

        got = await cache.get_window("c3", limit=2)
        assert [m["content"] for m in got] == ["m3", "m4"]

    @pytest.mark.asyncio
    async def test_miss_returns_none(self):
        cache = _make_cache()
        assert await cache.get_window("no-such-key") is None

    @pytest.mark.asyncio
    async def test_invalidate(self):
        cache = _make_cache()
        await cache.append("c4", "user", "问题")
        assert await cache.invalidate("c4") is True
        assert await cache.get_window("c4") is None

    @pytest.mark.asyncio
    async def test_disabled_degrades_silently(self):
        from app.memory.short_term_cache import ShortTermCache

        cache = ShortTermCache(redis_url="redis://faketest")
        cache._enabled = False
        assert await cache.append("c5", "user", "q") is False
        assert await cache.get_window("c5") is None
        assert await cache.invalidate("c5") is False

    @pytest.mark.asyncio
    async def test_replace_rebuilds(self):
        cache = _make_cache()
        ok = await cache.replace(
            "c6",
            [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ],
        )
        assert ok is True
        assert await cache.get_window("c6") == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]


# ----------------------------------------------------------------------
# P2b 事实提取负规则
# ----------------------------------------------------------------------


class _CapturingLLM:
    """捕获 prompt 并返回固定响应的 LLM 替身。"""

    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    async def chat(self, messages, stream=True, max_tokens=None, **kwargs):
        self.prompts.append(messages[0]["content"])
        yield self.response


class TestP2bNegativeRulesInPrompt:
    """事实提取 prompt 必须显式包含负规则（寒暄/知识库内容/工具结果/AI 回复）。"""

    @pytest.mark.asyncio
    async def test_prompt_contains_negative_rules(self, monkeypatch):
        llm = _CapturingLLM("NONE")
        monkeypatch.setattr(
            "app.memory.memory_manager.get_llm_provider", lambda: llm
        )
        mgr = MemoryManager(db=MagicMock(), sidecar=None)

        long_msg = "请帮我查一下报销流程的文档，然后总结要点给我" * 4
        result = await mgr._llm_extract_facts(
            uuid.uuid4(), [{"role": "user", "content": long_msg}]
        )

        assert result == []  # NONE → 不提取
        prompt = llm.prompts[0]
        assert "负规则" in prompt
        assert "寒暄" in prompt
        assert "知识库" in prompt
        assert "工具" in prompt
        assert "AI 回复" in prompt

    @pytest.mark.asyncio
    async def test_short_conversation_skipped(self, monkeypatch):
        """对话过短不调用 LLM（也无需负规则）。"""
        llm = _CapturingLLM("NONE")
        monkeypatch.setattr(
            "app.memory.memory_manager.get_llm_provider", lambda: llm
        )
        mgr = MemoryManager(db=MagicMock(), sidecar=None)

        result = await mgr._llm_extract_facts(
            uuid.uuid4(), [{"role": "user", "content": "太短"}]
        )
        assert result == []
        assert llm.prompts == []  # 未调用 LLM
