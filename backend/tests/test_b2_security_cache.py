"""批次二测试 — RAG 主链路安全与缓存正确性。

覆盖修复点：
- PermissionService.get_accessible_kb_ids（admin 不限制 / 普通用户集合 / 空集合）；
- PermissionService.filter_retrieval_candidates（kb 归属 + 密级双重过滤）；
- engine._retrieve 显式空 kb_ids 短路（不回落全库检索）；
- engine.answer 空 kb_ids 跳过 FAQ 快捷匹配；
- 请求级 permission_filter 优先于构造级注入；
- 忠实度拦截重生成答案：ANSWER_REGENERATED 事件 + 缓存写入新答案；
- 低置信 / 被拦截答案不写入缓存；
- shortcut_handler 快捷路径权限过滤（重排前 + 异常保守返回空）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.rag.engine import AgenticRAGEngine
from app.services.permission_service import PermissionService
from app.utils.sse import SSEEvent, SSEEventType


# ======================================================================
# Mock 组件（与 test_rag_engine.py 同款风格）
# ======================================================================


class FakeLLM:
    """Mock LLM Provider — 按预设文本响应 chat 调用。"""

    def __init__(self, response: str = "generate") -> None:
        self.response = response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        yield self.response


class FakeRetriever:
    """Mock HybridRetriever — 记录 search 调用。"""

    def __init__(self, candidates: list[dict[str, Any]] | None = None) -> None:
        self.candidates = candidates or []
        self.search_calls: list[dict[str, Any]] = []

    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls.append({"query": query, "kb_ids": kb_ids, "top_k": top_k, "filters": filters})
        return self.candidates


class FakeReranker:
    """Mock Reranker — 按 index 契约返回。"""

    def __init__(self) -> None:
        self.called = False
        self.received_docs: list[dict[str, Any]] = []

    async def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        self.called = True
        self.received_docs = list(documents)
        return [
            {"index": i, "score": 0.9 - i * 0.1, "content": d.get("content", "")}
            for i, d in enumerate(documents)
        ]


class FakeGenerator:
    """Mock Generator — 逐 token yield 预设文本。"""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["旧", "答案"]

    async def generate(
        self,
        query: str,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str = "",
        constraint_context: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        for token in self.tokens:
            yield token


class FakeMCPClient:
    async def get_tools_for_llm(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        return "{}"


class FakeCache:
    """Mock TokenCache — 记录 set 调用。"""

    def __init__(self) -> None:
        self.set_calls: list[dict[str, Any]] = []

    async def get(
        self,
        query: str,
        tenant_id: str | None = None,
        scope: str | None = None,
    ) -> None:
        return None

    async def set(
        self,
        query: str,
        answer: str,
        tenant_id: str | None = None,
        doc_ids: list[str] | None = None,
        scope: str | None = None,
    ) -> None:
        self.set_calls.append(
            {
                "query": query,
                "answer": answer,
                "tenant_id": tenant_id,
                "doc_ids": doc_ids,
                "scope": scope,
            }
        )


def _make_engine(
    *,
    candidates: list[dict[str, Any]] | None = None,
    cache: FakeCache | None = None,
    permission_filter: Any = None,
    faq_matcher: Any = None,
) -> tuple[AgenticRAGEngine, FakeRetriever, FakeReranker]:
    """构造带 Mock 组件的引擎（禁用 FAQ 自动初始化，注入空 matcher）。"""
    retriever = FakeRetriever(candidates)
    reranker = FakeReranker()
    engine = AgenticRAGEngine(
        llm=FakeLLM("generate"),
        mcp_client=FakeMCPClient(),
        retriever=retriever,
        reranker=reranker,
        generator=FakeGenerator(),
        cache=cache,
        permission_filter=permission_filter,
        faq_matcher=faq_matcher,  # None → 引擎会尝试自动初始化，测试中显式覆盖
    )
    # 测试中显式控制 FAQ matcher（避免真实 OpenSearch 依赖）
    engine._faq_matcher = faq_matcher
    return engine, retriever, reranker


def _make_state(**overrides: Any) -> dict:
    state: dict[str, Any] = {
        "query": "测试查询",
        "rewritten_query": None,
        "user_id": "user-001",
        "session_id": "session-001",
        "messages": [],
        "retrieved_docs": [],
        "tool_results": [],
        "answer": "",
        "iteration": 0,
        "max_iterations": 5,
        "kb_ids": None,
        "memory_context": "",
        "permission_filter": None,
    }
    state.update(overrides)
    return state


# ======================================================================
# PermissionService.get_accessible_kb_ids
# ======================================================================


class TestGetAccessibleKbIds:
    """get_accessible_kb_ids 语义：admin → None；普通用户 → 集合（可空）。"""

    @pytest.mark.asyncio
    async def test_admin_returns_none(self) -> None:
        """admin 返回 None（表示不限制，检索全部知识库）。"""
        user = SimpleNamespace(role="admin", clearance_level="internal", id=uuid4())
        service = PermissionService(db=AsyncMock(), user=user)

        result = await service.get_accessible_kb_ids()

        assert result is None

    @pytest.mark.asyncio
    async def test_normal_user_returns_set(self) -> None:
        """普通用户返回 DB 查询到的可访问 kb_id 集合。"""
        kb_id = uuid4()
        user = SimpleNamespace(role="editor", clearance_level="internal", id=uuid4())

        db = AsyncMock()
        exec_result = MagicMock()
        exec_result.all.return_value = [(kb_id,)]
        db.execute = AsyncMock(return_value=exec_result)
        service = PermissionService(db=db, user=user)

        result = await service.get_accessible_kb_ids()

        assert result == {kb_id}

    @pytest.mark.asyncio
    async def test_normal_user_empty_set(self) -> None:
        """普通用户无可访问知识库时返回空集合（而非 None）。"""
        user = SimpleNamespace(role="viewer", clearance_level="public", id=uuid4())

        db = AsyncMock()
        exec_result = MagicMock()
        exec_result.all.return_value = []
        db.execute = AsyncMock(return_value=exec_result)
        service = PermissionService(db=db, user=user)

        result = await service.get_accessible_kb_ids()

        assert result == set()


# ======================================================================
# PermissionService.filter_retrieval_candidates
# ======================================================================


class TestFilterRetrievalCandidates:
    """filter_retrieval_candidates：Final Gate 三项复检（I1 状态 + I3 密级 + I4 归属）。

    execute 调用序列（Phase 0 升级后）：
        1. _load_doc_meta → [(doc_id, classification, status), ...]（全部角色）
        2. get_accessible_kb_ids → [(kb_id,), ...]（非 admin）
    """

    @pytest.mark.asyncio
    async def test_admin_passes_published_only(self) -> None:
        """admin 放行 kb / 密级维度，但 I1 状态复检对 admin 同样生效。"""
        user = SimpleNamespace(role="admin", clearance_level="internal", id=uuid4())
        doc_ok, doc_draft = uuid4(), uuid4()

        db = AsyncMock()
        meta_result = MagicMock()
        # secret 密级 admin 放行；draft 状态任何角色剔除
        meta_result.all.return_value = [
            (doc_ok, "secret", "published"),
            (doc_draft, "public", "draft"),
        ]
        db.execute = AsyncMock(side_effect=[meta_result])
        service = PermissionService(db=db, user=user)

        candidates = [
            {"doc_id": str(doc_ok), "kb_id": str(uuid4()), "content": "a"},
            {"doc_id": str(doc_draft), "kb_id": str(uuid4()), "content": "b"},
        ]

        result = await service.filter_retrieval_candidates(candidates)

        assert [r["doc_id"] for r in result] == [str(doc_ok)]

    @pytest.mark.asyncio
    async def test_empty_accessible_set_returns_empty(self) -> None:
        """可访问集合为空时，所有候选被剔除。"""
        user = SimpleNamespace(role="viewer", clearance_level="public", id=uuid4())

        db = AsyncMock()
        exec_result = MagicMock()
        exec_result.all.return_value = []  # 可访问 kb 集合为空
        db.execute = AsyncMock(return_value=exec_result)
        service = PermissionService(db=db, user=user)

        candidates = [{"doc_id": str(uuid4()), "kb_id": str(uuid4()), "content": "a"}]

        result = await service.filter_retrieval_candidates(candidates)

        assert result == []

    @pytest.mark.asyncio
    async def test_kb_ownership_and_clearance_filter(self) -> None:
        """非可访问知识库 + 密级超限的候选均被剔除。"""
        kb_allowed = uuid4()
        kb_other = uuid4()
        doc_ok = uuid4()
        doc_secret = uuid4()
        user = SimpleNamespace(role="editor", clearance_level="internal", id=uuid4())

        db = AsyncMock()

        # 第一次 execute：_load_doc_meta → doc_ok=internal/published,
        # doc_secret=secret/published（另两个候选 doc_id 查不到 → fail-closed）
        meta_result = MagicMock()
        meta_result.all.return_value = [
            (doc_ok, "internal", "published"),
            (doc_secret, "secret", "published"),
        ]
        # 第二次 execute：get_accessible_kb_ids → 返回 kb_allowed
        kb_result = MagicMock()
        kb_result.all.return_value = [(kb_allowed,)]
        db.execute = AsyncMock(side_effect=[meta_result, kb_result])

        service = PermissionService(db=db, user=user)

        candidates = [
            {"doc_id": str(doc_ok), "kb_id": str(kb_allowed), "content": "可见"},
            {"doc_id": str(doc_secret), "kb_id": str(kb_allowed), "content": "密级超限"},
            {"doc_id": str(uuid4()), "kb_id": str(kb_other), "content": "越权知识库"},
            {"doc_id": str(uuid4()), "kb_id": None, "content": "kb_id 缺失"},
        ]

        result = await service.filter_retrieval_candidates(candidates)

        assert len(result) == 1
        assert result[0]["content"] == "可见"

    @pytest.mark.asyncio
    async def test_missing_doc_classification_fail_closed(self) -> None:
        """DB 查不到文档记录时保守剔除（fail-closed）。

        索引中残留的已删除/越权文档分块若按 internal 默认放行，
        会对低密级用户泄漏高密级内容 — 必须与 kb 维度一致 fail-closed。
        Phase 0 升级后 _load_doc_meta 首查（先于 kb 集合查询），
        记录查不到 → I1 状态未知 → 全部剔除，不再进入后续维度。
        """
        kb_allowed = uuid4()
        doc_id = uuid4()
        user = SimpleNamespace(role="editor", clearance_level="internal", id=uuid4())

        db = AsyncMock()
        meta_result = MagicMock()
        meta_result.all.return_value = []  # 查不到 → 保守剔除
        db.execute = AsyncMock(side_effect=[meta_result])

        service = PermissionService(db=db, user=user)
        candidates = [{"doc_id": str(doc_id), "kb_id": str(kb_allowed), "content": "x"}]

        result = await service.filter_retrieval_candidates(candidates)

        # 文档记录未知（含状态/密级）→ fail-closed 剔除
        assert len(result) == 0


# ======================================================================
# engine._retrieve 空 kb_ids 短路
# ======================================================================


class TestEmptyKbIdsShortCircuit:
    """显式空 kb_ids（用户无可访问知识库）必须短路，不得回落全库检索。"""

    @pytest.mark.asyncio
    async def test_retrieve_empty_kb_ids_skips_search(self) -> None:
        """_retrieve 收到空 kb_ids 时不调用 retriever.search，结果为空。"""
        engine, retriever, reranker = _make_engine(
            candidates=[{"chunk_id": "1", "content": "doc"}]
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=[])

        assert retriever.search_calls == []
        assert not reranker.called
        assert state["retrieved_docs"] == []

    @pytest.mark.asyncio
    async def test_retrieve_none_kb_ids_searches_all(self) -> None:
        """kb_ids=None（admin 不限制）时正常走检索。"""
        engine, retriever, _ = _make_engine(
            candidates=[{"chunk_id": "1", "content": "doc"}]
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=None)

        assert len(retriever.search_calls) == 1
        assert retriever.search_calls[0]["kb_ids"] is None

    @pytest.mark.asyncio
    async def test_answer_empty_kb_ids_skips_faq(self) -> None:
        """answer 收到空 kb_ids 时跳过 FAQ 快捷匹配（防跨知识库泄漏）。"""
        faq_matcher = AsyncMock()
        faq_matcher.match = AsyncMock(
            return_value=SimpleNamespace(matched=True, answer="FAQ答案", score=1.0, chunk_id="c1", doc_id="d1")
        )
        engine, _, _ = _make_engine(faq_matcher=faq_matcher)

        chunks = []
        async for chunk in engine.answer("q", "user-1", "session-1", kb_ids=[]):
            chunks.append(chunk)

        faq_matcher.match.assert_not_called()
        # 短路后仍完整走完生成流程（generate 路径产出 token）
        assert any(isinstance(c, str) for c in chunks)


# ======================================================================
# 请求级 permission_filter 优先级
# ======================================================================


class TestRequestLevelPermissionFilter:
    """请求级 permission_filter（携带用户上下文）优先于构造级注入。"""

    @pytest.mark.asyncio
    async def test_request_level_filter_wins(self) -> None:
        """state 中的请求级 filter 被调用，构造级 filter 不被调用。"""
        calls: list[str] = []

        async def ctor_filter(docs: list[dict]) -> list[dict]:
            calls.append("ctor")
            return docs

        async def request_filter(docs: list[dict]) -> list[dict]:
            calls.append("request")
            return [d for d in docs if d.get("keep")]

        engine, _, reranker = _make_engine(
            candidates=[
                {"chunk_id": "1", "content": "保留", "keep": True},
                {"chunk_id": "2", "content": "剔除", "keep": False},
            ],
            permission_filter=ctor_filter,
        )
        state = _make_state(permission_filter=request_filter)

        await engine._retrieve(state, kb_ids=None)

        assert calls == ["request"]
        assert len(reranker.received_docs) == 1
        assert reranker.received_docs[0]["content"] == "保留"

    @pytest.mark.asyncio
    async def test_filter_error_returns_empty(self) -> None:
        """请求级 filter 异常时保守返回空（不泄露越权文档）。"""

        async def error_filter(docs: list[dict]) -> list[dict]:
            raise RuntimeError("permission service down")

        engine, _, reranker = _make_engine(
            candidates=[{"chunk_id": "1", "content": "doc"}],
        )
        state = _make_state(permission_filter=error_filter)

        await engine._retrieve(state, kb_ids=None)

        assert not reranker.called
        assert state["retrieved_docs"] == []


# ======================================================================
# 重生成答案 SSE + 缓存
# ======================================================================


class TestRegeneratedAnswer:
    """忠实度拦截重生成答案：事件推送 + 缓存写新答案。"""

    @pytest.mark.asyncio
    async def test_regenerated_answer_event_and_cache(self) -> None:
        """_reflect 重生成答案后：
        1. yield ANSWER_REGENERATED 事件携带完整新答案；
        2. 缓存写入的是新答案（而非已流出的旧答案）。
        """
        cache = FakeCache()
        engine, _, _ = _make_engine(cache=cache)

        async def fake_reflect(state: dict) -> None:
            state["answer"] = "重生成的新答案"
            state["answer_regenerated"] = True
            return None

        engine._reflect = fake_reflect  # type: ignore[method-assign]

        events: list[SSEEvent] = []
        tokens: list[str] = []
        async for chunk in engine.answer("q", "user-1", "session-1", tenant_id="t1"):
            if isinstance(chunk, SSEEvent):
                events.append(chunk)
            elif isinstance(chunk, str):
                tokens.append(chunk)

        # 旧答案 token 已流出（客户端流式展示）
        assert "".join(tokens) == "旧答案"
        # ANSWER_REGENERATED 事件携带完整新答案
        regen_events = [e for e in events if e.event == SSEEventType.ANSWER_REGENERATED]
        assert len(regen_events) == 1
        assert regen_events[0].data == {"answer": "重生成的新答案"}
        # 缓存写入新答案
        assert len(cache.set_calls) == 1
        assert cache.set_calls[0]["answer"] == "重生成的新答案"
        assert cache.set_calls[0]["tenant_id"] == "t1"

    @pytest.mark.asyncio
    async def test_cache_doc_ids_from_retrieved_docs(self) -> None:
        """缓存写入携带 retrieved_docs 的 doc_ids（文档更新时主动失效）。"""
        cache = FakeCache()
        doc_id = uuid4()
        engine, _, _ = _make_engine(cache=cache)

        async def fake_reflect(state: dict) -> None:
            state["retrieved_docs"] = [{"doc_id": str(doc_id), "content": "x"}]
            return None

        engine._reflect = fake_reflect  # type: ignore[method-assign]

        async for _ in engine.answer("q", "user-1", "session-1"):
            pass

        assert len(cache.set_calls) == 1
        assert cache.set_calls[0]["doc_ids"] == [str(doc_id)]


class TestLowQualityCacheSkip:
    """低置信 / 被拦截答案不写入缓存。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "block_flag",
        ["low_confidence", "contradiction_blocked", "high_risk_blocked"],
    )
    async def test_blocked_answer_not_cached(self, block_flag: str) -> None:
        """设置任一拦截标记时，cache.set 不被调用。"""
        cache = FakeCache()
        engine, _, _ = _make_engine(cache=cache)

        async def fake_reflect(state: dict) -> None:
            state[block_flag] = True
            return None

        engine._reflect = fake_reflect  # type: ignore[method-assign]

        async for _ in engine.answer("q", "user-1", "session-1"):
            pass

        assert cache.set_calls == []

    @pytest.mark.asyncio
    async def test_normal_answer_cached(self) -> None:
        """无拦截标记时正常写入缓存（对照组）。"""
        cache = FakeCache()
        engine, _, _ = _make_engine(cache=cache)

        async def fake_reflect(state: dict) -> None:
            return None

        engine._reflect = fake_reflect  # type: ignore[method-assign]

        async for _ in engine.answer("q", "user-1", "session-1"):
            pass

        assert len(cache.set_calls) == 1
        assert cache.set_calls[0]["answer"] == "旧答案"


# ======================================================================
# shortcut_handler 快捷路径权限过滤
# ======================================================================


class TestShortcutPermissionFilter:
    """快捷路径：权限过滤在重排之前；过滤异常保守返回空。"""

    def _make_handler(self) -> Any:
        from app.intent.shortcut_handler import ShortcutHandler

        handler = ShortcutHandler()
        return handler

    @pytest.mark.asyncio
    async def test_permission_filter_applied_before_rerank(self) -> None:
        """shortcut 路径应用 permission_filter，越权文档不进入重排。"""
        from app.intent.router import IntentResult, IntentType

        handler = self._make_handler()
        retriever = FakeRetriever(
            [
                {"chunk_id": "1", "content": "公开", "keep": True},
                {"chunk_id": "2", "content": "机密", "keep": False},
            ]
        )
        reranker = FakeReranker()
        handler._retriever = retriever
        handler._reranker = reranker

        # generator 也 mock 掉，避免真实 LLM 调用
        handler._generator = FakeGenerator(["回答"])

        async def permission_filter(docs: list[dict]) -> list[dict]:
            return [d for d in docs if d.get("keep")]

        intent = IntentResult(intent=IntentType.RAG_SEARCH, confidence=0.95, use_shortcut=True)
        user = SimpleNamespace(id=uuid4(), role="editor")

        chunks = []
        async for chunk in handler.handle(
            intent=intent,
            query="测试",
            user=user,
            db=AsyncMock(),
            kb_ids=None,
            permission_filter=permission_filter,
        ):
            chunks.append(chunk)

        # 重排器只收到过滤后的 1 条文档
        assert reranker.called
        assert len(reranker.received_docs) == 1
        assert reranker.received_docs[0]["content"] == "公开"

    @pytest.mark.asyncio
    async def test_permission_filter_error_returns_no_candidates(self) -> None:
        """shortcut 路径权限过滤异常时，候选清空（走"未找到"分支）。"""
        from app.intent.router import IntentResult, IntentType

        handler = self._make_handler()
        retriever = FakeRetriever([{"chunk_id": "1", "content": "doc"}])
        reranker = FakeReranker()
        handler._retriever = retriever
        handler._reranker = reranker
        handler._generator = FakeGenerator(["未找到相关信息"])

        async def error_filter(docs: list[dict]) -> list[dict]:
            raise RuntimeError("permission down")

        intent = IntentResult(intent=IntentType.RAG_SEARCH, confidence=0.95, use_shortcut=True)
        user = SimpleNamespace(id=uuid4(), role="editor")

        chunks = []
        async for chunk in handler.handle(
            intent=intent,
            query="测试",
            user=user,
            db=AsyncMock(),
            kb_ids=None,
            permission_filter=error_filter,
        ):
            chunks.append(chunk)

        # 过滤异常 → 候选清空 → 重排器未被调用
        assert not reranker.called
        # retrieve_end 事件 doc_count=0
        end_events = [
            c for c in chunks
            if isinstance(c, SSEEvent) and c.event == SSEEventType.RETRIEVE_END
        ]
        assert end_events and end_events[0].data["doc_count"] == 0
