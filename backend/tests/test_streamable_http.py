"""
StreamableHTTP 传输层综合测试 — 对齐 MCP 2026-07-28 规范。

测试覆盖范围：
1. protocol.py — JSON-RPC 2.0 协议编解码
2. streamable_http.py — 传输层路由（同步/异步/SSE）
3. FastAPI 端点 — POST /mcp JSON-RPC 入口
4. MCPClient JSON-RPC 方法
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.base import Tool
from app.mcp.client import MCPClient
from app.mcp.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    METHOD_TASKS_CANCEL,
    METHOD_TASKS_CREATE,
    METHOD_TASKS_GET,
    METHOD_NOTIFICATION_INITIALIZED,
    PARSE_ERROR,
    INVALID_REQUEST,
    INTERNAL_ERROR,
    TOOL_NOT_FOUND,
    TOOL_EXECUTION_ERROR,
    TASK_NOT_FOUND,
    TASK_ALREADY_TERMINAL,
    SUPPORTED_MCP_METHODS,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    parse_request,
    make_success_response,
    make_error_response,
    make_notification_response,
    make_tools_list_params,
    make_tools_call_params,
    make_tasks_create_params,
    make_tasks_get_params,
    make_tasks_cancel_params,
)
from app.mcp.streamable_http import StreamableHTTPTransport, sse_serialize
from app.mcp.task_store import TaskStore


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def mock_server():
    """创建模拟的 KnowledgeBaseMCPServer。"""
    server = MagicMock()

    # 工具列表
    server.list_tools = AsyncMock(return_value=[
        Tool(
            name="knowledge_search",
            description="搜索知识库",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="query_oa_approval",
            description="查询 OA 审批状态",
            parameters={
                "type": "object",
                "properties": {
                    "bill_no": {"type": "string", "description": "单据编号"},
                },
                "required": ["bill_no"],
            },
        ),
        Tool(
            name="batch_analyze_documents",
            description="批量分析文档",
            parameters={
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "知识库 ID"},
                },
                "required": ["kb_id"],
            },
        ),
    ])

    # 同步工具调用
    async def _call_tool(tool_name: str, arguments: dict, *, tenant_id=None):
        if tool_name == "knowledge_search":
            return json.dumps({"results": [{"id": "doc-1", "title": "Test Doc"}], "count": 1})
        if tool_name == "query_oa_approval":
            return json.dumps({"bill_no": arguments.get("bill_no", ""), "status": "processing"})
        return json.dumps({"error": f"未知工具: {tool_name}"})

    server.call_tool = AsyncMock(side_effect=_call_tool)

    # 长耗时标记
    def _is_long_running(tool_name: str) -> bool:
        return tool_name in ("query_oa_approval", "batch_analyze_documents")

    server.is_long_running = MagicMock(side_effect=_is_long_running)

    # 异步工具调用
    async def _call_tool_async(tool_name: str, arguments: dict, *, tenant_id=None):
        return json.dumps({
            "task_id": "test-task-123",
            "status": "working",
            "poll_interval_ms": 2000,
            "ttl_ms": 3600000,
        })

    server.call_tool_async = AsyncMock(side_effect=_call_tool_async)

    return server


@pytest.fixture
def task_store():
    """创建内存版 TaskStore（不依赖 Redis）。"""
    return TaskStore(redis_url=None)


@pytest.fixture
def transport(mock_server, task_store):
    """创建 StreamableHTTPTransport 实例（使用 mock server + 内存 TaskStore）。"""
    return StreamableHTTPTransport(mock_server, task_store=task_store)


@pytest.fixture
def client(mock_server, transport):
    """创建 MCPClient 实例（使用 mock server）。"""
    c = MCPClient(mock_server)
    c._transport = transport
    return c


# ======================================================================
# 1. protocol.py — JSON-RPC 2.0 协议编解码
# ======================================================================


class TestProtocolParseRequest:
    """测试 parse_request 函数。"""

    def test_valid_request_with_dict_params(self):
        """有效请求：dict 参数 + 字符串 id。"""
        raw = '{"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": "1"}'
        req = parse_request(raw)
        assert req.jsonrpc == "2.0"
        assert req.method == "tools/list"
        assert req.params == {}
        assert req.id == "1"

    def test_valid_request_with_list_params(self):
        """有效请求：list 参数 + 数字 id。"""
        raw = '{"jsonrpc": "2.0", "method": "tools/call", "params": ["arg1", "arg2"], "id": 2}'
        req = parse_request(raw)
        assert req.method == "tools/call"
        assert req.params == ["arg1", "arg2"]
        assert req.id == 2

    def test_valid_request_no_params(self):
        """有效请求：无 params 字段。"""
        raw = '{"jsonrpc": "2.0", "method": "tools/list", "id": "1"}'
        req = parse_request(raw)
        assert req.method == "tools/list"
        assert req.params is None
        assert req.id == "1"

    def test_notification(self):
        """通知：id 为 null。"""
        raw = '{"jsonrpc": "2.0", "method": "notifications/initialized"}'
        req = parse_request(raw)
        assert req.method == "notifications/initialized"
        assert req.is_notification()

    def test_invalid_json(self):
        """无效 JSON。"""
        raw = "not-json"
        with pytest.raises(JSONRPCError) as exc:
            parse_request(raw)
        assert exc.value.code == PARSE_ERROR

    def test_invalid_jsonrpc_version(self):
        """错误的 jsonrpc 版本。"""
        raw = '{"jsonrpc": "1.0", "method": "tools/list", "id": "1"}'
        with pytest.raises(JSONRPCError) as exc:
            parse_request(raw)
        assert exc.value.code == INVALID_REQUEST

    def test_missing_method(self):
        """缺少 method 字段。"""
        raw = '{"jsonrpc": "2.0", "id": "1"}'
        with pytest.raises(JSONRPCError) as exc:
            parse_request(raw)
        assert exc.value.code == INVALID_REQUEST

    def test_empty_method(self):
        """空字符串 method。"""
        raw = '{"jsonrpc": "2.0", "method": "", "id": "1"}'
        with pytest.raises(JSONRPCError) as exc:
            parse_request(raw)
        assert exc.value.code == INVALID_REQUEST

    def test_invalid_params_type(self):
        """params 必须是 object 或 array。"""
        raw = '{"jsonrpc": "2.0", "method": "tools/list", "params": "invalid", "id": "1"}'
        with pytest.raises(JSONRPCError) as exc:
            parse_request(raw)
        assert exc.value.code == INVALID_REQUEST

    def test_invalid_id_type(self):
        """id 必须是 string/number/null。"""
        raw = '{"jsonrpc": "2.0", "method": "tools/list", "id": true}'
        with pytest.raises(JSONRPCError) as exc:
            parse_request(raw)
        assert exc.value.code == INVALID_REQUEST

    def test_parse_request_from_dict(self):
        """从已解析的字典解析。"""
        data = {"jsonrpc": "2.0", "method": "tools/list", "id": "1"}
        req = parse_request(data)
        assert req.method == "tools/list"
        assert req.id == "1"


class TestProtocolResponse:
    """测试 JSON-RPC 响应构造。"""

    def test_success_response(self):
        """成功响应。"""
        resp = make_success_response(
            result={"tools": [{"name": "test"}]},
            request_id="1",
        )
        assert resp.jsonrpc == "2.0"
        assert resp.result == {"tools": [{"name": "test"}]}
        assert resp.error is None
        assert resp.id == "1"

    def test_success_response_no_id(self):
        """通知成功响应（id 为 None）。"""
        resp = make_success_response(result=None, request_id=None)
        assert resp.id is None

    def test_error_response(self):
        """错误响应。"""
        resp = make_error_response(
            code=METHOD_NOT_FOUND,
            message="Method not found",
            request_id="1",
        )
        assert resp.jsonrpc == "2.0"
        assert resp.result is None
        assert resp.error is not None
        assert resp.error.code == METHOD_NOT_FOUND
        assert resp.error.message == "Method not found"
        assert resp.id == "1"

    def test_error_response_with_data(self):
        """错误响应含附加数据。"""
        resp = make_error_response(
            code=INVALID_PARAMS,
            message="Invalid params",
            data={"param": "query", "reason": "missing"},
            request_id="1",
        )
        assert resp.error.data == {"param": "query", "reason": "missing"}

    def test_notification_response(self):
        """通知响应。"""
        resp = make_notification_response()
        assert resp.id is None
        assert resp.result is None
        assert resp.error is None

    def test_response_to_dict(self):
        """序列化为字典。"""
        resp = make_success_response(result={"key": "value"}, request_id="1")
        d = resp.to_dict()
        assert d["jsonrpc"] == "2.0"
        assert d["result"] == {"key": "value"}
        assert d["id"] == "1"
        assert "error" not in d

    def test_error_response_to_dict(self):
        """错误响应序列化为字典。"""
        resp = make_error_response(code=METHOD_NOT_FOUND, request_id="1")
        d = resp.to_dict()
        assert "error" in d
        assert d["error"]["code"] == METHOD_NOT_FOUND
        assert "result" not in d

    def test_response_to_json(self):
        """序列化为 JSON 字符串。"""
        resp = make_success_response(result={"key": "value"}, request_id="1")
        s = resp.to_json()
        parsed = json.loads(s)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["result"] == {"key": "value"}

    def test_error_to_dict(self):
        """错误对象序列化。"""
        error = JSONRPCError(code=PARSE_ERROR, message="Parse error", data={"detail": "bad json"})
        d = error.to_dict()
        assert d["code"] == PARSE_ERROR
        assert d["message"] == "Parse error"
        assert d["data"] == {"detail": "bad json"}


class TestProtocolHelpers:
    """测试协议辅助函数。"""

    def test_make_tools_list_params(self):
        params = make_tools_list_params()
        assert params == {}

    def test_make_tools_call_params(self):
        params = make_tools_call_params("test_tool", {"arg": "value"})
        assert params == {"name": "test_tool", "arguments": {"arg": "value"}}

    def test_make_tasks_create_params(self):
        params = make_tasks_create_params("test_tool", {"arg": "value"})
        assert params == {"tool": "test_tool", "arguments": {"arg": "value"}}

    def test_make_tasks_get_params(self):
        params = make_tasks_get_params("task-123")
        assert params == {"task_id": "task-123"}

    def test_make_tasks_cancel_params(self):
        params = make_tasks_cancel_params("task-123")
        assert params == {"task_id": "task-123"}

    def test_supported_methods(self):
        """支持的方法列表完整。"""
        assert METHOD_TOOLS_LIST in SUPPORTED_MCP_METHODS
        assert METHOD_TOOLS_CALL in SUPPORTED_MCP_METHODS
        assert METHOD_TASKS_CREATE in SUPPORTED_MCP_METHODS
        assert METHOD_TASKS_GET in SUPPORTED_MCP_METHODS
        assert METHOD_TASKS_CANCEL in SUPPORTED_MCP_METHODS
        assert METHOD_NOTIFICATION_INITIALIZED in SUPPORTED_MCP_METHODS

    def test_request_is_notification(self):
        """通知判断。"""
        req = JSONRPCRequest(method="notifications/initialized", id=None)
        assert req.is_notification()

        req = JSONRPCRequest(method="tools/list", id="1")
        assert not req.is_notification()


# ======================================================================
# 2. streamable_http.py — 传输层路由
# ======================================================================


class TestStreamableHTTPTransport:
    """测试 StreamableHTTPTransport 传输层。"""

    @pytest.mark.asyncio
    async def test_tools_list(self, transport):
        """tools/list：返回工具列表。"""
        raw_body = '{"jsonrpc": "2.0", "method": "tools/list", "id": "1"}'
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "1"
        assert result.error is None
        assert result.result is not None
        assert "tools" in result.result
        assert len(result.result["tools"]) == 3
        assert result.result["tools"][0]["name"] == "knowledge_search"

    @pytest.mark.asyncio
    async def test_tools_call_sync(self, transport):
        """tools/call：同步工具调用。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "knowledge_search", "arguments": {"query": "test"}},
            "id": "2",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "2"
        assert result.error is None
        assert result.result is not None
        assert "content" in result.result
        assert result.result["isError"] is False

    @pytest.mark.asyncio
    async def test_tools_call_missing_name(self, transport):
        """tools/call：缺少工具名称。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"arguments": {}},
            "id": "3",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_tools_call_unknown_tool(self, transport):
        """tools/call：未知工具（MCP Server 兜底返回 error JSON）。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
            "id": "4",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "4"
        # Server 将未知工具转为 error JSON 字符串，传输层视为成功响应
        assert result.error is None
        assert result.result is not None
        assert "content" in result.result
        result_text = json.loads(result.result["content"][0]["text"])
        assert "error" in result_text

    @pytest.mark.asyncio
    async def test_tools_call_long_running_async(self, transport):
        """tools/call：长耗时工具返回 SSE 流。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "query_oa_approval", "arguments": {"bill_no": "BG2024001"}},
            "id": "5",
        })
        result = await transport.handle_request(raw_body)

        # 长耗时工具应返回 AsyncIterator
        assert hasattr(result, "__aiter__")

        responses = []
        async for resp in result:
            responses.append(resp)

        # 至少应有 working 和 completed/failed 两个事件
        assert len(responses) >= 2
        assert responses[0].result["status"] == "working"
        assert responses[0].result["task_id"] is not None
        # 最后一个响应应为终态
        assert responses[-1].result["status"] in ("completed", "failed")

    @pytest.mark.asyncio
    async def test_tasks_get(self, transport):
        """tasks/get：查询任务状态。"""
        # 先创建一个任务
        task_id = await transport._task_store.create_task(
            tool_name="test_tool",
            arguments={"key": "value"},
        )

        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {"task_id": task_id},
            "id": "6",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "6"
        assert result.error is None
        assert result.result["status"] == "working"
        assert result.result["task_id"] == task_id

    @pytest.mark.asyncio
    async def test_tasks_get_not_found(self, transport):
        """tasks/get：任务不存在。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {"task_id": "nonexistent-task"},
            "id": "7",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == TASK_NOT_FOUND

    @pytest.mark.asyncio
    async def test_tasks_get_missing_task_id(self, transport):
        """tasks/get：缺少 task_id。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {},
            "id": "8",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_tasks_cancel(self, transport):
        """tasks/cancel：取消任务。"""
        # 先创建一个任务
        task_id = await transport._task_store.create_task(
            tool_name="test_tool",
            arguments={},
        )

        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/cancel",
            "params": {"task_id": task_id},
            "id": "9",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "9"
        assert result.error is None
        assert result.result["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_tasks_cancel_not_found(self, transport):
        """tasks/cancel：任务不存在。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/cancel",
            "params": {"task_id": "nonexistent"},
            "id": "10",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == TASK_NOT_FOUND

    @pytest.mark.asyncio
    async def test_tasks_cancel_already_terminal(self, transport):
        """tasks/cancel：任务已终态。"""
        # 创建一个任务并完成它
        task_id = await transport._task_store.create_task(
            tool_name="test_tool",
            arguments={},
        )
        await transport._task_store.complete_task(task_id, {"result": "done"})

        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/cancel",
            "params": {"task_id": task_id},
            "id": "11",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == TASK_ALREADY_TERMINAL

    @pytest.mark.asyncio
    async def test_tasks_cancel_missing_task_id(self, transport):
        """tasks/cancel：缺少 task_id。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/cancel",
            "params": {},
            "id": "12",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_tasks_create(self, transport):
        """tasks/create：创建任务。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/create",
            "params": {"tool": "query_oa_approval", "arguments": {"bill_no": "BG2024001"}},
            "id": "13",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "13"
        assert result.error is None
        assert result.result is not None
        assert "task_id" in json.dumps(result.result)

    @pytest.mark.asyncio
    async def test_tasks_create_missing_tool(self, transport):
        """tasks/create：缺少 tool 参数。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/create",
            "params": {"arguments": {}},
            "id": "14",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_notification_initialized(self, transport):
        """notifications/initialized：通知。"""
        raw_body = '{"jsonrpc": "2.0", "method": "notifications/initialized"}'
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.id is None
        assert result.error is None
        assert result.result is None

    @pytest.mark.asyncio
    async def test_unknown_method(self, transport):
        """未知方法。"""
        raw_body = '{"jsonrpc": "2.0", "method": "unknown/method", "id": "15"}'
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_malformed_json(self, transport):
        """格式错误的 JSON。"""
        raw_body = "not-json-at-all"
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is not None
        assert result.error.code == PARSE_ERROR

    @pytest.mark.asyncio
    async def test_tasks_get_completed_task(self, transport):
        """tasks/get：已完成的任务。"""
        # 创建任务并完成
        task_id = await transport._task_store.create_task(
            tool_name="test_tool",
            arguments={"key": "value"},
        )
        await transport._task_store.complete_task(task_id, {"result_data": "success"})

        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {"task_id": task_id},
            "id": "16",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is None
        assert result.result["status"] == "completed"
        assert result.result["result"] == {"result_data": "success"}

    @pytest.mark.asyncio
    async def test_tasks_get_failed_task(self, transport):
        """tasks/get：失败的任务。"""
        # 创建任务并标记失败
        task_id = await transport._task_store.create_task(
            tool_name="test_tool",
            arguments={},
        )
        await transport._task_store.fail_task(task_id, "Something went wrong")

        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tasks/get",
            "params": {"task_id": task_id},
            "id": "17",
        })
        result = await transport.handle_request(raw_body)

        assert isinstance(result, JSONRPCResponse)
        assert result.error is None
        assert result.result["status"] == "failed"
        assert "error" in result.result


class TestSSESerialization:
    """测试 SSE 序列化。"""

    def test_sse_serialize_success(self):
        """成功响应的 SSE 序列化。"""
        resp = make_success_response(result={"key": "value"}, request_id="1")
        sse = sse_serialize(resp)
        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")
        parsed = json.loads(sse[6:].strip())
        assert parsed["result"] == {"key": "value"}

    def test_sse_serialize_error(self):
        """错误响应的 SSE 序列化。"""
        sse = sse_serialize(
            make_error_response(code=METHOD_NOT_FOUND, request_id="1"),
        )
        assert sse.startswith("data: ")
        parsed = json.loads(sse[6:].strip())
        assert "error" in parsed


# ======================================================================
# 3. MCPClient JSON-RPC 方法
# ======================================================================


class TestMCPClientJsonRPC:
    """测试 MCPClient 的 JSON-RPC 协议方法。"""

    @pytest.mark.asyncio
    async def test_jsonrpc_tools_list(self, client):
        """jsonrpc_tools_list：列出工具。"""
        resp = await client.jsonrpc_tools_list(request_id="1")
        assert resp.id == "1"
        assert resp.error is None
        assert resp.result is not None
        assert "tools" in resp.result
        assert len(resp.result["tools"]) == 3

    @pytest.mark.asyncio
    async def test_jsonrpc_tools_call_sync(self, client):
        """jsonrpc_tools_call：同步工具调用。"""
        resp = await client.jsonrpc_tools_call(
            tool_name="knowledge_search",
            arguments={"query": "test"},
            request_id="2",
        )
        assert resp.id == "2"
        assert resp.error is None
        assert resp.result is not None
        assert "content" in resp.result

    @pytest.mark.asyncio
    async def test_jsonrpc_tools_call_long_running(self, client):
        """jsonrpc_tools_call：长耗时工具。"""
        resp = await client.jsonrpc_tools_call(
            tool_name="query_oa_approval",
            arguments={"bill_no": "BG2024001"},
            request_id="3",
        )
        assert resp.id == "3"
        # 长耗时工具返回最终状态（迭代到最后一条）
        assert resp.result is not None
        assert resp.result["status"] in ("completed", "failed")

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_get(self, client, transport):
        """jsonrpc_tasks_get：查询任务。"""
        # 先创建任务
        task_id = await transport._task_store.create_task(
            tool_name="test_tool",
            arguments={},
        )

        resp = await client.jsonrpc_tasks_get(task_id=task_id, request_id="4")
        assert resp.id == "4"
        assert resp.error is None
        assert resp.result["task_id"] == task_id
        assert resp.result["status"] == "working"

    @pytest.mark.asyncio
    async def test_jsonrpc_tasks_cancel(self, client, transport):
        """jsonrpc_tasks_cancel：取消任务。"""
        # 先创建任务
        task_id = await transport._task_store.create_task(
            tool_name="test_tool",
            arguments={},
        )

        resp = await client.jsonrpc_tasks_cancel(task_id=task_id, request_id="5")
        assert resp.id == "5"
        assert resp.error is None
        assert resp.result["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_jsonrpc_call_unknown_method(self, client):
        """jsonrpc_call：未知方法。"""
        resp = await client.jsonrpc_call(
            method="unknown/method",
            request_id="99",
        )
        assert resp.id == "99"
        assert resp.error is not None
        assert resp.error.code == METHOD_NOT_FOUND


# ======================================================================
# 4. 集成测试：FastAPI 端点
# ======================================================================


class TestFastAPIJsonRPCEndpoint:
    """测试 FastAPI POST /mcp 端点。"""

    @pytest.mark.asyncio
    async def test_mcp_endpoint_tools_list(self, client):
        """通过 StreamableHTTPTransport 模拟完整的 tools/list 请求。"""
        raw_body = '{"jsonrpc": "2.0", "method": "tools/list", "id": "1"}'
        transport = client._transport
        result = await transport.handle_request(raw_body, tenant_id=None)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "1"
        assert result.error is None
        assert "tools" in result.result
        d = result.to_dict()
        assert d["jsonrpc"] == "2.0"
        assert "result" in d
        assert "tools" in d["result"]

    @pytest.mark.asyncio
    async def test_mcp_endpoint_tools_call(self, client):
        """通过 StreamableHTTPTransport 模拟 tools/call 请求。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "knowledge_search", "arguments": {"query": "test"}},
            "id": "2",
        })
        transport = client._transport
        result = await transport.handle_request(raw_body, tenant_id=None)

        assert isinstance(result, JSONRPCResponse)
        assert result.id == "2"
        d = result.to_dict()
        assert "result" in d

    @pytest.mark.asyncio
    async def test_sse_streaming_contains_tool_result(self, client):
        """SSE 流式响应包含工具结果。"""
        raw_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "query_oa_approval", "arguments": {"bill_no": "BG2024001"}},
            "id": "3",
        })
        transport = client._transport
        result = await transport.handle_request(raw_body, tenant_id=None)

        # 验证是 SSE 流
        assert hasattr(result, "__aiter__")

        # 收集所有响应
        responses = []
        async for resp in result:
            d = resp.to_dict()
            responses.append(d)

        # 验证 SSE 流结构
        assert len(responses) >= 2
        assert responses[0]["result"]["status"] == "working"
        assert responses[-1]["result"]["status"] in ("completed", "failed")

        # 验证 SSE 序列化
        for resp_data in responses:
            sse = sse_serialize(
                JSONRPCResponse(
                    jsonrpc=resp_data["jsonrpc"],
                    result=resp_data.get("result"),
                    error=resp_data.get("error"),
                    id=resp_data.get("id"),
                ),
            )
            assert sse.startswith("data: ")
            assert sse.endswith("\n\n")