"""批次四并发与性能修复测试 — 四路并行检索 / usage 并发隔离 / MCP 租户传播。

覆盖：
- HybridRetriever.search：四路检索 asyncio.gather 真并行、异常兜底净化
- Generator.last_usage：ContextVar 按 asyncio 任务隔离，并发请求互不污染
- MCPClient.call_tool_from_llm：tenant_id 透传 server.call_tool
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
# B4-1: 四路检索并行执行
# ======================================================================


class TestRetrieverParallelExecution:
    """四路检索必须真并行（asyncio.gather），而非顺序 await。"""

    def _make_retriever(self) -> Any:
        from app.rag.retriever import HybridRetriever

        mock_http = MagicMock()
        mock_http.aclose = AsyncMock()
        return HybridRetriever(
            embedder=MagicMock(),
            http_client=mock_http,
            vector_store=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_four_paths_run_concurrently(self) -> None:
        """四路检索重叠执行 — 用事件计数证明并行而非顺序。

        每个子方法进入时计数 +1，await 一个 asyncio.Event（永不设置，
        靠超时放行）模拟 I/O 等待；若为顺序执行，最大同时在跑数恒为 1，
        真并行时四路会同时挂起，峰值计数达到 4。
        """
        retriever = self._make_retriever()

        entered = 0
        peak = 0
        gate = asyncio.Event()

        async def _path(name: str) -> list[dict[str, Any]]:
            nonlocal entered, peak
            entered += 1
            peak = max(peak, entered)
            try:
                await asyncio.wait_for(gate.wait(), timeout=0.05)
            except TimeoutError:
                pass
            entered -= 1
            return []

        retriever._vector_search = lambda q, k, t: _path("vector")  # type: ignore[method-assign]
        retriever._fulltext_search = lambda q, k, t: _path("fulltext")  # type: ignore[method-assign]
        retriever._cross_modal_search = lambda q, k, t: _path("cross_modal")  # type: ignore[method-assign]
        retriever._graph_search = lambda e, k, t: _path("graph")  # type: ignore[method-assign]

        await retriever.search("并行测试", top_k=5)

        assert peak == 4, f"四路未并行（峰值并发={peak}，顺序执行为 1）"

    @pytest.mark.asyncio
    async def test_parallel_latency_less_than_sequential_sum(self) -> None:
        """并行总延迟 ≈ 最慢一路，而非四路之和。"""
        retriever = self._make_retriever()

        async def _slow_path(*args: Any) -> list[dict[str, Any]]:
            await asyncio.sleep(0.05)
            return []

        retriever._vector_search = _slow_path  # type: ignore[method-assign]
        retriever._fulltext_search = _slow_path  # type: ignore[method-assign]
        retriever._cross_modal_search = _slow_path  # type: ignore[method-assign]
        retriever._graph_search = _slow_path  # type: ignore[method-assign]

        import time

        t0 = time.monotonic()
        await retriever.search("延迟测试", top_k=5)
        elapsed = time.monotonic() - t0

        # 顺序执行需 ≥ 0.2s；并行应 < 0.15s（留出调度余量）
        assert elapsed < 0.15, f"疑似顺序执行：{elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_results_merged_across_four_paths(self) -> None:
        """四路结果正常合并去重。"""
        retriever = self._make_retriever()

        def _doc(chunk_id: str, score: float, source: str) -> dict[str, Any]:
            return {
                "doc_id": f"doc_{chunk_id}",
                "chunk_id": chunk_id,
                "content": f"内容_{chunk_id}",
                "score": score,
                "source": source,
                "kb_id": None,
                "title": None,
            }

        retriever._vector_search = AsyncMock(return_value=[_doc("c1", 0.9, "vector")])  # type: ignore[method-assign]
        retriever._fulltext_search = AsyncMock(return_value=[_doc("c2", 0.8, "fulltext")])  # type: ignore[method-assign]
        retriever._cross_modal_search = AsyncMock(return_value=[_doc("c3", 0.7, "cross_modal")])  # type: ignore[method-assign]
        retriever._graph_search = AsyncMock(return_value=[_doc("c4", 0.6, "graph")])  # type: ignore[method-assign]

        results = await retriever.search("合并测试", top_k=10)

        chunk_ids = {r["chunk_id"] for r in results}
        assert chunk_ids == {"c1", "c2", "c3", "c4"}

    @pytest.mark.asyncio
    async def test_single_path_exception_degrades_to_empty(self) -> None:
        """单路逃逸异常 → 该路降级空列表，其余路正常返回。"""
        retriever = self._make_retriever()

        async def _boom(*args: Any) -> list[dict[str, Any]]:
            raise RuntimeError("模拟子方法逃逸异常")

        retriever._vector_search = _boom  # type: ignore[method-assign]
        retriever._fulltext_search = AsyncMock(return_value=[{
            "doc_id": "d1", "chunk_id": "c1", "content": "x",
            "score": 0.5, "source": "fulltext", "kb_id": None, "title": None,
        }])  # type: ignore[method-assign]
        retriever._cross_modal_search = AsyncMock(return_value=[])  # type: ignore[method-assign]
        retriever._graph_search = AsyncMock(return_value=[])  # type: ignore[method-assign]

        results = await retriever.search("异常测试", top_k=5)

        assert len(results) == 1
        assert results[0]["chunk_id"] == "c1"


class TestEnsureList:
    """_ensure_list 兜底净化逻辑。"""

    def test_exception_returns_empty(self) -> None:
        from app.rag.retriever import HybridRetriever

        assert HybridRetriever._ensure_list(RuntimeError("x"), "vector") == []

    def test_non_list_returns_empty(self) -> None:
        from app.rag.retriever import HybridRetriever

        assert HybridRetriever._ensure_list("not-a-list", "vector") == []
        assert HybridRetriever._ensure_list(None, "vector") == []

    def test_list_passthrough(self) -> None:
        from app.rag.retriever import HybridRetriever

        data = [{"chunk_id": "c1"}]
        assert HybridRetriever._ensure_list(data, "vector") is data


# ======================================================================
# B4-2: Generator.last_usage 并发隔离
# ======================================================================


class _FakeLLM:
    """按请求标记返回不同 usage 的假 LLM Provider。

    query 以 "A" 开头 → usage input=100；以 "B" 开头 → input=200。
    流中途插入可等待点，制造并发交错窗口。
    """

    def __init__(self, stall: asyncio.Event | None = None) -> None:
        self._stall = stall

    async def chat(self, messages: list[dict], stream: bool = True, **kw: Any) -> Any:
        query = messages[-1]["content"]
        yield "答案片段"
        if self._stall is not None:
            await self._stall.wait()
        usage_in = 100 if query.startswith("A") else 200
        yield {"type": "usage", "input_tokens": usage_in, "output_tokens": 10, "model": "fake"}


class TestGeneratorUsageConcurrencyIsolation:
    """last_usage 基于 ContextVar — 并发请求各自读写，互不污染。"""

    @pytest.mark.asyncio
    async def test_concurrent_generate_usage_isolated(self) -> None:
        """两个并发 generate 各自读到自己的 usage，不串请求。"""
        from app.rag.generator import Generator

        stall = asyncio.Event()
        generator = Generator(llm=_FakeLLM(stall=stall))  # type: ignore[arg-type]

        async def _run(query: str) -> dict[str, Any] | None:
            tokens: list[str] = []
            async for tok in generator.generate(query, [], []):
                tokens.append(tok)
            return generator.last_usage

        task_a = asyncio.create_task(_run("A 问题"))
        task_b = asyncio.create_task(_run("B 问题"))
        # 两个任务都进入流并挂起后放行，制造交错
        await asyncio.sleep(0.02)
        stall.set()
        usage_a, usage_b = await asyncio.gather(task_a, task_b)

        assert usage_a is not None and usage_a["input_tokens"] == 100
        assert usage_b is not None and usage_b["input_tokens"] == 200

    @pytest.mark.asyncio
    async def test_sequential_generate_resets_usage(self) -> None:
        """同任务顺序调用：每次 generate 重置 usage，无残留。"""
        from app.rag.generator import Generator

        generator = Generator(llm=_FakeLLM())  # type: ignore[arg-type]

        async for _ in generator.generate("A 问题", [], []):
            pass
        assert generator.last_usage is not None
        assert generator.last_usage["input_tokens"] == 100

        async for _ in generator.generate("B 问题", [], []):
            pass
        assert generator.last_usage is not None
        assert generator.last_usage["input_tokens"] == 200

    @pytest.mark.asyncio
    async def test_usage_set_in_task_not_visible_in_creator(self) -> None:
        """任务内 set 的 usage 不泄漏到创建方上下文（隔离的核心语义）。

        create_task 在创建时拷贝当前 context；generate 内 ContextVar.set
        只作用于消费该生成器的任务副本，不回传创建方。因此创建方读到的
        仍是默认值 None — 这正是并发请求互不污染的保证。
        """
        from app.rag.generator import Generator

        generator = Generator(llm=_FakeLLM())  # type: ignore[arg-type]

        async def _run_in_task() -> dict[str, Any] | None:
            async for _ in generator.generate("A 问题", [], []):
                pass
            return generator.last_usage

        usage_in_task = await asyncio.create_task(_run_in_task())

        # 任务 A 内部能读到自己的 usage
        assert usage_in_task is not None
        assert usage_in_task["input_tokens"] == 100
        # 创建方上下文未被污染 — 仍为 None
        assert generator.last_usage is None

    def test_property_setter_getter_sync(self) -> None:
        """同步上下文 property 读写正常（向后兼容 engine getattr 读取）。"""
        from app.rag.generator import Generator

        generator = Generator(llm=_FakeLLM())  # type: ignore[arg-type]
        assert generator.last_usage is None
        generator.last_usage = {"input_tokens": 1}
        assert generator.last_usage == {"input_tokens": 1}
        generator.last_usage = None
        assert generator.last_usage is None


# ======================================================================
# B4-3: MCP call_tool_from_llm 租户传播
# ======================================================================


class TestMCPCallToolFromLLMTenant:
    """call_tool_from_llm 必须把调用方 tenant_id 透传到 server.call_tool。"""

    def _make_client(self) -> tuple[Any, AsyncMock]:
        from app.mcp.client import MCPClient

        server = MagicMock()
        server.call_tool = AsyncMock(return_value='{"ok": true}')
        client = MCPClient(server=server)
        return client, server.call_tool

    @pytest.mark.asyncio
    async def test_tenant_id_propagated(self) -> None:
        """传入 tenant_id → server.call_tool 收到相同 tenant_id。"""
        client, mock_call = self._make_client()

        tool_use = {"type": "tool_use", "id": "tu_1", "name": "knowledge_search", "input": {"query": "报销"}}
        result = await client.call_tool_from_llm(tool_use, tenant_id="t-uuid-1")

        assert result == '{"ok": true}'
        mock_call.assert_awaited_once_with(
            "knowledge_search", {"query": "报销"}, tenant_id="t-uuid-1"
        )

    @pytest.mark.asyncio
    async def test_no_tenant_passes_none(self) -> None:
        """不传 tenant_id → 透传 None（向后兼容，server 侧走不过滤兜底）。"""
        client, mock_call = self._make_client()

        tool_use = {"type": "tool_use", "id": "tu_2", "name": "knowledge_search", "input": {}}
        await client.call_tool_from_llm(tool_use)

        mock_call.assert_awaited_once_with("knowledge_search", {}, tenant_id=None)

    @pytest.mark.asyncio
    async def test_llm_input_tenant_not_trusted(self) -> None:
        """LLM 在 input 中自封的 tenant_id 不得被当作租户上下文。

        租户上下文只能来自调用方显式参数；arguments 原样透传给工具
        （server 端 _tenant_ctx 由 call_tool 的 tenant_id 参数设置，
        与 arguments 内容无关）。
        """
        client, mock_call = self._make_client()

        tool_use = {
            "type": "tool_use",
            "id": "tu_3",
            "name": "knowledge_search",
            "input": {"query": "x", "tenant_id": "forged-by-llm"},
        }
        await client.call_tool_from_llm(tool_use, tenant_id="real-tenant")

        # arguments 原样透传，但 tenant_id 上下文是调用方给的 real-tenant
        mock_call.assert_awaited_once_with(
            "knowledge_search",
            {"query": "x", "tenant_id": "forged-by-llm"},
            tenant_id="real-tenant",
        )
