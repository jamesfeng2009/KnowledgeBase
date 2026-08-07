"""批次五测试 — answer_with_graph 错误文本处理与缓存质量门禁 + 测试基建修复。

覆盖修复点：
- answer_with_graph 图执行异常（无已流出 token）→ 原样抛出，
  不再 yield 错误文本（防错误被持久化/写缓存/泄漏内部细节）；
- answer_with_graph 图执行异常（已有部分 token 流出）→ 静默结束，
  不追加错误文本、不写缓存；
- 低置信 / 被拦截（矛盾 block / 高风险 block）答案不写缓存
  （从图最终状态读取质量标记，与 answer() 主链路门禁一致）；
- 正常答案写缓存并携带 doc_ids（回归保护）；
- 重生成答案以图最终状态为准写缓存（回归保护）。

注：测试环境未安装 langgraph，通过 patch LANGGRAPH_AVAILABLE=True
并注入假 compiled graph（astream / aget_state）驱动 answer_with_graph。
"""
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery（与其他测试文件保持一致的导入防护）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# Mock 组件
# ======================================================================


class FakeLLM:
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        yield "generate"


class FakeRetriever:
    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        return []


class FakeReranker:
    async def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        return []


class FakeGenerator:
    async def generate(
        self,
        query: str,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str = "",
    ) -> AsyncIterator[str]:
        yield "答案"


class FakeMCPClient:
    async def get_tools_for_llm(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        return "{}"


class FakeCache:
    """Mock TokenCache — 记录 set 调用，get 始终未命中。"""

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
            {"query": query, "answer": answer, "tenant_id": tenant_id, "doc_ids": doc_ids}
        )


class FakeSnapshot:
    """模拟 LangGraph StateSnapshot — 仅携带 values。"""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values


class FakeCompiledGraph:
    """模拟编译后的 StateGraph — 按脚本驱动 astream 更新流。

    Args:
        updates: 依次 yield 的节点状态增量（stream_mode="updates" 格式）。
        error_at: 在 yield 第 N 个更新前抛出异常（None 表示不抛）。
        final_values: aget_state 返回的最终状态 values。
    """

    def __init__(
        self,
        updates: list[dict[str, Any]],
        *,
        error_at: int | None = None,
        final_values: dict[str, Any] | None = None,
    ) -> None:
        self._updates = updates
        self._error_at = error_at
        self._final_values = final_values or {}

    async def astream(
        self,
        state: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "updates",
    ) -> AsyncIterator[dict[str, Any]]:
        for idx, update in enumerate(self._updates):
            if self._error_at is not None and idx == self._error_at:
                raise RuntimeError("模拟图节点执行失败")
            yield update
        # 全部 yield 完后若 error_at == len(updates)，末尾抛异常
        if self._error_at is not None and self._error_at >= len(self._updates):
            raise RuntimeError("模拟图末尾执行失败")

    async def aget_state(self, config: dict[str, Any]) -> FakeSnapshot:
        return FakeSnapshot(self._final_values)


def _make_engine(cache: FakeCache | None, graph: FakeCompiledGraph) -> Any:
    """构造注入假 compiled graph 的引擎（绕过真实 LangGraph 依赖）。"""
    from app.rag.engine import AgenticRAGEngine

    engine = AgenticRAGEngine(
        llm=FakeLLM(),  # type: ignore[arg-type]
        mcp_client=FakeMCPClient(),  # type: ignore[arg-type]
        retriever=FakeRetriever(),  # type: ignore[arg-type]
        reranker=FakeReranker(),  # type: ignore[arg-type]
        generator=FakeGenerator(),  # type: ignore[arg-type]
        cache=cache,  # type: ignore[arg-type]
    )
    engine._faq_matcher = None
    engine._get_or_build_graph = lambda: graph  # type: ignore[method-assign]
    return engine


async def _collect(engine: Any, query: str = "测试问题") -> list[str]:
    tokens: list[str] = []
    async for token in engine.answer_with_graph(
        query, user_id="u1", session_id="s1", tenant_id="t1"
    ):
        tokens.append(token)
    return tokens


def _patch_langgraph_available() -> Any:
    return patch("app.rag.engine.LANGGRAPH_AVAILABLE", True)


# ======================================================================
# B5-1: 错误文本处理
# ======================================================================


class TestGraphErrorHandling:
    """图执行异常不再作为答案文本 yield。"""

    @pytest.mark.asyncio
    async def test_error_before_any_token_raises(self) -> None:
        """无 token 流出时异常原样抛出（不 yield 错误文本）。"""
        graph = FakeCompiledGraph(
            updates=[{"_stream_tokens": ["答", "案"]}],
            error_at=0,  # 第一个更新前即失败
        )
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            with pytest.raises(RuntimeError, match="模拟图节点执行失败"):
                await _collect(engine)

        assert cache.set_calls == []

    @pytest.mark.asyncio
    async def test_error_after_partial_tokens_returns_silently(self) -> None:
        """已有部分 token 流出 → 静默结束：不抛异常、不追加错误文本、不写缓存。"""
        graph = FakeCompiledGraph(
            updates=[
                {"_stream_tokens": ["部分", "答案"]},
                {"_stream_tokens": ["不应到达"]},
            ],
            error_at=1,  # 第二个更新前失败
        )
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            tokens = await _collect(engine)

        assert tokens == ["部分", "答案"]
        # 回归保护：旧实现会 yield "[Graph 执行出错: ...]" 作为答案文本
        assert not any("执行出错" in t for t in tokens)
        assert cache.set_calls == []

    @pytest.mark.asyncio
    async def test_error_text_never_in_stream(self) -> None:
        """任何路径下流出 token 均不含内部错误细节（防泄漏/防持久化）。"""
        graph = FakeCompiledGraph(updates=[], error_at=0)
        engine = _make_engine(FakeCache(), graph)

        with _patch_langgraph_available():
            try:
                tokens = await _collect(engine)
            except RuntimeError:
                tokens = []

        assert not any("Graph" in t or "出错" in t for t in tokens)


# ======================================================================
# B5-1: 缓存质量门禁
# ======================================================================


class TestGraphCacheQualityGate:
    """低质量 / 被拦截答案不写缓存（读取图最终状态标记）。"""

    def _graph_with_answer(self, final_values: dict[str, Any]) -> FakeCompiledGraph:
        return FakeCompiledGraph(
            updates=[
                {"retrieved_docs": [{"doc_id": "d1", "chunk_id": "c1"}]},
                {"_stream_tokens": ["完整", "答案"]},
            ],
            final_values=final_values,
        )

    @pytest.mark.asyncio
    async def test_low_confidence_not_cached(self) -> None:
        """图最终状态 low_confidence=True → 不写缓存。"""
        graph = self._graph_with_answer({
            "answer": "完整答案",
            "low_confidence": True,
        })
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            tokens = await _collect(engine)

        assert tokens == ["完整", "答案"]
        assert cache.set_calls == []

    @pytest.mark.asyncio
    async def test_contradiction_blocked_not_cached(self) -> None:
        """图最终状态 contradiction_blocked=True → 不写缓存。"""
        graph = self._graph_with_answer({
            "answer": "完整答案",
            "contradiction_blocked": True,
        })
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            await _collect(engine)

        assert cache.set_calls == []

    @pytest.mark.asyncio
    async def test_high_risk_blocked_not_cached(self) -> None:
        """图最终状态 high_risk_blocked=True → 不写缓存。"""
        graph = self._graph_with_answer({
            "answer": "完整答案",
            "high_risk_blocked": True,
        })
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            await _collect(engine)

        assert cache.set_calls == []

    @pytest.mark.asyncio
    async def test_normal_answer_cached_with_doc_ids(self) -> None:
        """正常答案写缓存 — 携带 doc_ids（回归保护）。"""
        graph = self._graph_with_answer({"answer": "完整答案"})
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            await _collect(engine)

        assert len(cache.set_calls) == 1
        call = cache.set_calls[0]
        assert call["answer"] == "完整答案"
        assert call["doc_ids"] == ["d1"]
        assert call["tenant_id"] == "t1"

    @pytest.mark.asyncio
    async def test_regenerated_final_answer_cached(self) -> None:
        """reflect 重生成后缓存写最终答案而非流式拼接（回归保护）。"""
        graph = FakeCompiledGraph(
            updates=[{"_stream_tokens": ["旧", "答案"]}],
            final_values={"answer": "重生成的新答案"},
        )
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            tokens = await _collect(engine)

        assert tokens == ["旧", "答案"]  # 流式回放不变
        assert cache.set_calls[0]["answer"] == "重生成的新答案"

    @pytest.mark.asyncio
    async def test_aget_state_failure_falls_back_to_streamed_answer(self) -> None:
        """无法读取图最终状态时回退流式拼接答案（优雅降级）。"""
        graph = FakeCompiledGraph(
            updates=[{"_stream_tokens": ["流式", "答案"]}],
        )
        graph.aget_state = AsyncMock(side_effect=RuntimeError("snapshot 不可用"))  # type: ignore[method-assign]
        cache = FakeCache()
        engine = _make_engine(cache, graph)

        with _patch_langgraph_available():
            await _collect(engine)

        assert cache.set_calls[0]["answer"] == "流式答案"
