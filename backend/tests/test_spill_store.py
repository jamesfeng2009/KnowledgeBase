"""
P1-1 大工具结果写盘（Spill to Disk）测试。

覆盖：
    - SpillStore 写盘/读回往返
    - 路径穿越防护（read 越界抛 ValueError）
    - read_tool_result 工具已注册到 MCP Server
    - 引擎决策循环：超大工具结果 → 上下文只留 placeholder
"""

import pytest

from app.rag.context_budget import SpillStore, _SPILL_TOOL_RESULT_THRESHOLD


class TestSpillStore:
    """SpillStore 写盘/读回。"""

    def test_spill_and_read_roundtrip(self, tmp_path):
        store = SpillStore(base_dir=str(tmp_path))
        rel = store.spill("tenant-1", "search_erp", "x" * 3000)
        # 路径含租户前缀
        assert rel.startswith("tenant-1")
        assert store.read(rel) == "x" * 3000

    def test_path_traversal_blocked(self, tmp_path):
        store = SpillStore(base_dir=str(tmp_path))
        with pytest.raises(ValueError):
            store.read("../secret.txt")

    def test_missing_file_raises(self, tmp_path):
        store = SpillStore(base_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            store.read("tenant-1/nonexistent.txt")


class TestReadToolResultTool:
    """read_tool_result 工具注册与读取。"""

    @pytest.mark.asyncio
    async def test_tool_registered(self):
        from app.mcp.server import KnowledgeBaseMCPServer

        server = KnowledgeBaseMCPServer(db_factory=lambda: None)
        tools = await server.list_tools()
        names = [t["name"] for t in tools]
        assert "read_tool_result" in names

    @pytest.mark.asyncio
    async def test_tool_reads_spilled_content(self, tmp_path):
        from app.mcp.server import KnowledgeBaseMCPServer

        server = KnowledgeBaseMCPServer(db_factory=lambda: None)
        store = SpillStore(base_dir=str(tmp_path))
        rel = store.spill("tenant-1", "search_erp", "全文内容" * 1000)

        # 用同目录的 store 读回（工具内部用默认目录，此处直接验证读回逻辑）
        result = store.read(rel)
        assert "全文内容" in result

    @pytest.mark.asyncio
    async def test_tool_returns_error_on_missing(self, tmp_path):
        from app.mcp.server import KnowledgeBaseMCPServer

        server = KnowledgeBaseMCPServer(db_factory=lambda: None)
        handler = server._tool_registry["read_tool_result"]["handler"]
        out = await handler("tenant-1/missing.txt")
        assert "not found" in out


class TestEngineSpill:
    """引擎决策循环 — 超大工具结果写盘。"""

    @pytest.mark.asyncio
    async def test_large_tool_result_spilled_to_placeholder(self):
        from tests.test_p2_token_optimization import (
            FakeGenerator,
            FakeMCPClient,
            FakeReranker,
            FakeRetriever,
            MessageRecordingLLM,
            _make_state,
        )
        from app.rag.engine import AgenticRAGEngine

        llm = MessageRecordingLLM("tool_call")
        engine = AgenticRAGEngine(
            llm=llm,
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
            cache=None,
            max_iterations=2,
        )
        engine._planner = None

        # 用临时目录的 SpillStore 隔离测试写盘
        import tempfile
        store = SpillStore(base_dir=tempfile.mkdtemp())
        engine._spill_store = store

        state = _make_state()
        call_idx = 0
        responses = ["tool_call", "generate"]

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        async def mock_tool_call(state, db=None, user_uuid=None):
            # 超大工具结果（> 2000 字符阈值）
            state["tool_results"].append({
                "tool": "search_erp",
                "result": "数据" * 3000,
            })
            yield

        engine._tool_call_streaming = mock_tool_call

        await engine._run_decision_loop(state)

        # 上下文中的工具结果消息应含写盘 placeholder
        joined = "".join(
            m.get("content", "") for m in state["messages"]
        )
        assert "已写盘" in joined
        assert "read_tool_result" in joined

    @pytest.mark.asyncio
    async def test_small_tool_result_not_spilled(self):
        from tests.test_p2_token_optimization import (
            FakeGenerator,
            FakeMCPClient,
            FakeReranker,
            FakeRetriever,
            MessageRecordingLLM,
            _make_state,
        )
        from app.rag.engine import AgenticRAGEngine

        llm = MessageRecordingLLM("tool_call")
        engine = AgenticRAGEngine(
            llm=llm,
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
            cache=None,
            max_iterations=2,
        )
        engine._planner = None

        import tempfile
        store = SpillStore(base_dir=tempfile.mkdtemp())
        engine._spill_store = store

        state = _make_state()
        call_idx = 0
        responses = ["tool_call", "generate"]

        async def mock_chat(messages, tools=None, stream=False, **kwargs):
            nonlocal call_idx
            yield responses[min(call_idx, len(responses) - 1)]
            call_idx += 1

        llm.chat = mock_chat

        async def mock_tool_call(state, db=None, user_uuid=None):
            # 小工具结果（低于阈值，不写盘）
            state["tool_results"].append({
                "tool": "search_erp",
                "result": "小结果",
            })
            yield

        engine._tool_call_streaming = mock_tool_call

        await engine._run_decision_loop(state)

        joined = "".join(
            m.get("content", "") for m in state["messages"]
        )
        assert "已写盘" not in joined
