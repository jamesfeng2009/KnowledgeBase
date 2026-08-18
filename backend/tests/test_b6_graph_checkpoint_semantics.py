"""批次六测试 — LangGraph 图节点状态更新契约（checkpoint 语义锁）。

锁定 ``_graph_think`` 的双写契约（契约说明见 engine.py 该方法 docstring）：

1. 就地写 ``state["iteration"]`` 的唯一作用是让节点内调用的 ``_think()```
   读到新鲜轮次 — 框架不会把就地写合并进通道（测试 1 锁定：
   删除就地写 → spy 记录到旧轮次 → 失败）。
2. 节点返回的增量 dict 是通道 / checkpoint 的唯一权威更新 —
   LangGraph 只把返回值合并进通道状态（测试 2 / 3 锁定：
   删除返回值中的 iteration → aget_state 永远读到旧值 → 失败）。

FakeMergingGraph 精确模拟 LangGraph 通道合并语义：
- 节点收到工作状态的**副本**（节点内就地修改被框架丢弃）；
- 只有节点返回的增量 dict 被合并进通道状态，aget_state 读取该状态
  （即真实 checkpointer 持有的 values）。
"""
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

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
    async def generate(self, **kwargs: Any) -> AsyncIterator[str]:
        yield "答案"


class FakeMCPClient:
    async def get_tools_for_llm(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        return "{}"


class FakeCache:
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
            {"query": query, "answer": answer, "tenant_id": tenant_id}
        )


class FakeSnapshot:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values


class FakeMergingGraph:
    """模拟 LangGraph 通道合并语义的假编译图。

    - astream：将工作状态的副本传入真实 ``_graph_think`` 节点执行，
      仅把节点**返回的增量 dict** 合并进通道状态（就地写被丢弃），
      再按 ``_route_after_think`` 的路由结果执行 ``_graph_generate``；
    - aget_state：返回通道状态（即 checkpointer 持有的 values）。
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self.working: dict[str, Any] = {}
        #: spy 记录 _think 被调用时读到的 iteration 值
        self.think_observed_iterations: list[int] = []
        #: 记录条件路由函数基于合并后状态作出的路由决策
        self.routes: list[str] = []

    async def astream(
        self,
        state: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "updates",
    ) -> AsyncIterator[dict[str, Any]]:
        from app.rag.engine import AgenticRAGEngine

        self.working = dict(state)

        # think 节点 — spy 包裹 _think 记录其读到的轮次
        engine = self._engine
        orig_think = engine._think

        async def spy_think(s: dict[str, Any]) -> str:
            self.think_observed_iterations.append(int(s.get("iteration", -1)))
            return await orig_think(s)  # type: ignore[arg-type]

        engine._think = spy_think  # type: ignore[method-assign]
        try:
            node_state = dict(self.working)  # 副本：就地写被框架丢弃
            update = await engine._graph_think(node_state)
        finally:
            engine._think = orig_think  # type: ignore[method-assign]

        self.working.update(update)  # 仅返回值进入通道
        yield update

        # 条件路由 — 基于合并后的通道状态决策
        route = AgenticRAGEngine._route_after_think(self.working)
        self.routes.append(route)
        assert route == "generate", f"路由应走向 generate，实际 {route}"

        # generate 节点 — 同样仅返回值进入通道
        gen_update = await engine._graph_generate(self.working)
        self.working.update(gen_update)
        yield gen_update

    async def aget_state(self, config: dict[str, Any]) -> FakeSnapshot:
        return FakeSnapshot(self.working)


def _make_engine() -> Any:
    from app.rag.engine import AgenticRAGEngine

    engine = AgenticRAGEngine(
        llm=FakeLLM(),  # type: ignore[arg-type]
        mcp_client=FakeMCPClient(),  # type: ignore[arg-type]
        retriever=FakeRetriever(),  # type: ignore[arg-type]
        reranker=FakeReranker(),  # type: ignore[arg-type]
        generator=FakeGenerator(),  # type: ignore[arg-type]
        cache=FakeCache(),  # type: ignore[arg-type]
    )
    engine._faq_matcher = None
    return engine


def _patch_langgraph_available() -> Any:
    return patch("app.rag.engine.LANGGRAPH_AVAILABLE", True)


# ======================================================================
# B6: checkpoint 语义锁
# ======================================================================


class TestGraphCheckpointSemantics:
    """_graph_think 双写契约 — 就地写喂 _think，返回值进 checkpoint。"""

    @pytest.mark.asyncio
    async def test_intrathink_sees_fresh_iteration(self) -> None:
        """契约 1：就地写让节点内 _think 读到新鲜轮次（而非初始 0）。"""
        engine = _make_engine()
        graph = FakeMergingGraph(engine)
        engine._get_or_build_graph = lambda: graph  # type: ignore[method-assign]

        with _patch_langgraph_available():
            async for _ in engine.answer_with_graph(
                "测试问题", user_id="u1", session_id="s1"
            ):
                pass

        assert graph.think_observed_iterations == [1], (
            "_think 应读到就地写入后的新鲜轮次 1；"
            "若为 0 说明就地写被删除，违反契约 1"
        )

    @pytest.mark.asyncio
    async def test_checkpoint_iteration_comes_from_returned_dict(self) -> None:
        """契约 2：checkpoint 中的 iteration 只来自节点返回的增量。"""
        engine = _make_engine()
        graph = FakeMergingGraph(engine)
        engine._get_or_build_graph = lambda: graph  # type: ignore[method-assign]

        with _patch_langgraph_available():
            async for _ in engine.answer_with_graph(
                "测试问题", user_id="u1", session_id="s1"
            ):
                pass

        snapshot = await graph.aget_state({"configurable": {"thread_id": "s1"}})
        # 通道状态（= checkpointer 持有的 values）必须反映 think 节点
        # 返回值中的 iteration；若 _graph_think 只就地写不返回，
        # 此处会读到初始值 0，违反契约 2
        assert snapshot.values.get("iteration") == 1
        assert snapshot.values.get("answer") == "答案"

    @pytest.mark.asyncio
    async def test_e2e_answer_stream_and_cache(self) -> None:
        """端到端回归：token 回放、路由决策、缓存写入均正常。"""
        engine = _make_engine()
        cache: FakeCache = engine.cache  # type: ignore[assignment]
        graph = FakeMergingGraph(engine)
        engine._get_or_build_graph = lambda: graph  # type: ignore[method-assign]

        tokens: list[str] = []
        with _patch_langgraph_available():
            async for token in engine.answer_with_graph(
                "测试问题", user_id="u1", session_id="s1", tenant_id="t1"
            ):
                tokens.append(token)

        assert "".join(tokens) == "答案"
        assert graph.routes == ["generate"]
        assert len(cache.set_calls) == 1
        assert cache.set_calls[0]["answer"] == "答案"

    @pytest.mark.asyncio
    async def test_graph_think_return_shape(self) -> None:
        """返回增量形状锁定：恰好为 {"iteration", "_decision"} 两个字段。"""
        engine = _make_engine()
        state: dict[str, Any] = {
            "query": "测试",
            "iteration": 4,
            "max_iterations": 5,
            "messages": [],
            "retrieved_docs": [],
            "tool_results": [],
        }
        update = await engine._graph_think(state)

        assert set(update.keys()) == {"iteration", "_decision"}
        assert update["iteration"] == 5
        assert update["_decision"] in ("retrieve", "tool_call", "generate")
        # 就地写与返回值一致（同一轮次）
        assert state["iteration"] == 5
