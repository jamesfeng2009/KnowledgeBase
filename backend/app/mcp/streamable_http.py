"""
StreamableHTTP 传输层 — 对齐 MCP 2026-07-28 StreamableHTTP 规范。

将 JSON-RPC 2.0 请求路由到内部 KnowledgeBaseMCPServer 的方法调用，
支持同步响应和 SSE 流式响应两种模式。

架构：
```
HTTP POST /mcp  (JSON-RPC Body)
    ↓
parse_request() → JSONRPCRequest
    ↓
StreamableHTTPTransport.handle_request()
    ↓
    ├─ tools/list    → server.list_tools()
    ├─ tools/call    → server.call_tool() 或 server.call_tool_async()
    ├─ resources/list → server.list_resources()
    ├─ resources/read → server.read_resource()
    ├─ tasks/create  → server.call_tool_async()
    ├─ tasks/get     → task_store.get_task()
    ├─ tasks/cancel  → task_store.cancel_task()
    └─ notifications/initialized → 无需响应
    ↓
JSONRPCResponse → HTTP Response (JSON or SSE)
```

流式响应（SSE）:
- 仅 tools/call 方法在工具标记为 long_running 时使用 SSE
- SSE 格式：data: <JSON-RPC Response>\n\n
- 流式推送 intermediate 状态，最后推送 completed 终态

遵循单一职责：本模块只负责协议路由与传输格式转换，
不包含工具实现逻辑（由 KnowledgeBaseMCPServer 提供）。
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from app.mcp.protocol import (
    INVALID_PARAMS,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    METHOD_NOT_FOUND,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    METHOD_TASKS_CANCEL,
    METHOD_TASKS_CREATE,
    METHOD_TASKS_GET,
    METHOD_NOTIFICATION_INITIALIZED,
    INTERNAL_ERROR,
    RESOURCE_NOT_FOUND,
    TOOL_NOT_FOUND,
    TOOL_EXECUTION_ERROR,
    TASK_NOT_FOUND,
    TASK_ALREADY_TERMINAL,
    SUPPORTED_MCP_METHODS,
    make_success_response,
    make_error_response,
    make_notification_response,
    make_tools_list_params,
    make_tools_call_params,
    make_tasks_create_params,
    make_tasks_get_params,
    make_tasks_cancel_params,
    parse_request,
)
from app.mcp.server import KnowledgeBaseMCPServer
from app.mcp.task_store import TaskStore, get_task_store
from app.utils.logger import get_logger

log = get_logger(__name__)


class StreamableHTTPTransport:
    """StreamableHTTP 传输层 — 将 JSON-RPC 请求路由到 MCP Server。

    无状态设计：每个请求独立处理，不维护会话状态。
    对齐 MCP 2026-07-28 规范的无状态架构。
    """

    def __init__(
        self,
        server: KnowledgeBaseMCPServer,
        task_store: TaskStore | None = None,
    ) -> None:
        """初始化 StreamableHTTP 传输层。

        Args:
            server: MCP Server 实例，提供工具注册与分发能力。
            task_store: 任务状态存储（可选，从全局单例获取）。
        """
        self._server = server
        self._task_store = task_store or get_task_store()

    # ------------------------------------------------------------------
    # 请求处理入口
    # ------------------------------------------------------------------

    async def handle_request(
        self,
        raw_body: str | bytes,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse | AsyncIterator[JSONRPCResponse]:
        """处理单个 JSON-RPC 请求，返回响应。

        对于 tools/call 且工具标记为 long_running 时，
        返回 AsyncIterator 用于 SSE 流式输出。

        Args:
            raw_body: 原始 HTTP 请求体（JSON 字符串或字节）。
            tenant_id: 请求级租户 ID。

        Returns:
            JSONRPCResponse（同步响应）或 AsyncIterator（SSE 流式响应）。
        """
        # 1. 解析 JSON-RPC 请求
        try:
            request = parse_request(raw_body)
        except JSONRPCError as exc:
            return make_error_response(
                code=exc.code,
                message=exc.message,
                data=exc.data,
                request_id=None,
            )

        # 2. 校验 method 是否支持
        if request.method not in SUPPORTED_MCP_METHODS:
            return make_error_response(
                code=METHOD_NOT_FOUND,
                message=f"Method not found: {request.method}",
                data={"method": request.method},
                request_id=request.id,
            )

        # 3. 通知 — 无需响应
        if request.method == METHOD_NOTIFICATION_INITIALIZED:
            return self._handle_notification(request)

        # 4. 路由到对应处理方法
        try:
            if request.method == METHOD_TOOLS_LIST:
                return await self._handle_tools_list(request)
            elif request.method == METHOD_TOOLS_CALL:
                return await self._handle_tools_call(request, tenant_id=tenant_id)
            elif request.method == METHOD_RESOURCES_LIST:
                return await self._handle_resources_list(request)
            elif request.method == METHOD_RESOURCES_READ:
                return await self._handle_resources_read(request)
            elif request.method == METHOD_TASKS_CREATE:
                return await self._handle_tasks_create(request, tenant_id=tenant_id)
            elif request.method == METHOD_TASKS_GET:
                return await self._handle_tasks_get(request)
            elif request.method == METHOD_TASKS_CANCEL:
                return await self._handle_tasks_cancel(request)
            else:
                # 不应到达此处（已在 SUPPORTED_MCP_METHODS 校验）
                return make_error_response(
                    code=METHOD_NOT_FOUND,
                    request_id=request.id,
                )
        except JSONRPCError:
            raise
        except Exception as exc:
            log.error(
                "mcp.streamable_http.handler_error",
                method=request.method,
                error=str(exc),
            )
            return make_error_response(
                code=INTERNAL_ERROR,
                message=f"Internal error handling {request.method}",
                data=str(exc)[:500],
                request_id=request.id,
            )

    # ------------------------------------------------------------------
    # 方法路由
    # ------------------------------------------------------------------

    async def _handle_tools_list(
        self,
        request: JSONRPCRequest,
    ) -> JSONRPCResponse:
        """处理 tools/list 请求 — 返回工具列表。"""
        tools = await self._server.list_tools()

        # 转换为 MCP 规范的工具定义格式
        result = {
            "tools": [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("parameters", {}),
                }
                for t in tools
            ],
        }

        return make_success_response(
            result=result,
            request_id=request.id,
        )

    async def _handle_resources_list(
        self,
        request: JSONRPCRequest,
    ) -> JSONRPCResponse:
        """处理 resources/list 请求 — 返回资源列表（含 Resource Metadata）。"""
        resources = await self._server.list_resources()

        return make_success_response(
            result={"resources": resources},
            request_id=request.id,
        )

    async def _handle_resources_read(
        self,
        request: JSONRPCRequest,
    ) -> JSONRPCResponse:
        """处理 resources/read 请求 — 读取单个资源完整内容。

        请求 params 格式::
            {"uri": "resource://skill/knowledge_search"}
        """
        if not isinstance(request.params, dict):
            return make_error_response(
                code=INVALID_PARAMS,
                message="params must be a JSON object",
                request_id=request.id,
            )

        uri = request.params.get("uri", "")
        if not uri:
            return make_error_response(
                code=INVALID_PARAMS,
                message="'uri' is required in params",
                request_id=request.id,
            )

        resource = await self._server.read_resource(uri)
        if resource is None:
            return make_error_response(
                code=RESOURCE_NOT_FOUND,
                message=f"Resource not found: {uri}",
                request_id=request.id,
            )

        return make_success_response(
            result={"contents": [resource]},
            request_id=request.id,
        )

    async def _handle_tools_call(
        self,
        request: JSONRPCRequest,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse | AsyncIterator[JSONRPCResponse]:
        """处理 tools/call 请求。

        同步调用：直接返回工具结果。
        异步调用：对于 long_running 工具，启动后台任务并通过 SSE 流式推送。

        请求 params 格式::
            {"name": "tool_name", "arguments": {...}}
        """
        params = self._get_call_params(request)
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not tool_name:
            return make_error_response(
                code=INVALID_PARAMS,
                message="Tool name is required",
                request_id=request.id,
            )

        # 检查工具是否存在
        if not self._server.is_long_running(tool_name):
            return await self._handle_sync_tool_call(
                request, tool_name, arguments, tenant_id=tenant_id,
            )

        # 长耗时工具 → 异步创建任务，通过 SSE 流式推送
        # _handle_async_tool_call 是 async generator（yield 值），
        # 返回 generator 对象供 handle_request 识别为 SSE 流
        return self._handle_async_tool_call(
            request, tool_name, arguments, tenant_id=tenant_id,
        )

    async def _handle_sync_tool_call(
        self,
        request: JSONRPCRequest,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse:
        """同步调用工具 — 直接返回结果。"""
        try:
            result_str = await self._server.call_tool(
                tool_name, arguments, tenant_id=tenant_id,
            )
            try:
                result_data: Any = json.loads(result_str)
            except (json.JSONDecodeError, TypeError):
                result_data = result_str

            return make_success_response(
                result={
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result_data, ensure_ascii=False, default=str),
                        },
                    ],
                    "isError": False,
                },
                request_id=request.id,
            )
        except Exception as exc:
            log.error("mcp.tool_call_error", tool=tool_name, error=str(exc))
            return make_error_response(
                code=TOOL_EXECUTION_ERROR,
                message=f"Tool execution failed: {exc}",
                request_id=request.id,
            )

    async def _handle_async_tool_call(
        self,
        request: JSONRPCRequest,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str | None = None,
    ) -> AsyncIterator[JSONRPCResponse]:
        """异步调用长耗时工具 — 通过 SSE 流式推送中间和最终状态。

        先返回一个 working 状态，后台执行完成后推送 completed 状态。
        """
        # 创建任务
        task_id = await self._task_store.create_task(
            tool_name=tool_name,
            arguments=arguments,
            tenant_id=tenant_id,
        )

        # 推送初始状态
        yield make_success_response(
            result={
                "task_id": task_id,
                "status": "working",
                "poll_interval_ms": self._task_store.poll_interval_ms,
            },
            request_id=request.id,
        )

        # 后台执行
        try:
            result_str = await self._server.call_tool(
                tool_name, arguments, tenant_id=tenant_id,
            )
            try:
                result_data: Any = json.loads(result_str)
            except (json.JSONDecodeError, TypeError):
                result_data = result_str

            await self._task_store.complete_task(task_id, result_data)

            # 推送终态
            yield make_success_response(
                result={
                    "task_id": task_id,
                    "status": "completed",
                    "result": result_data,
                },
                request_id=request.id,
            )
        except Exception as exc:
            log.error("mcp.tool_call_async_error", tool=tool_name, error=str(exc))
            await self._task_store.fail_task(task_id, str(exc))

            # 推送失败状态
            yield make_success_response(
                result={
                    "task_id": task_id,
                    "status": "failed",
                    "error": str(exc),
                },
                request_id=request.id,
            )

    async def _handle_tasks_create(
        self,
        request: JSONRPCRequest,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse:
        """处理 tasks/create 请求 — 创建长耗时任务并返回任务句柄。

        请求 params 格式::
            {"tool": "tool_name", "arguments": {...}}
        """
        if not isinstance(request.params, dict):
            return make_error_response(
                code=INVALID_PARAMS,
                message="params must be a JSON object",
                request_id=request.id,
            )

        tool_name = request.params.get("tool", "")
        arguments = request.params.get("arguments", {})

        if not tool_name:
            return make_error_response(
                code=INVALID_PARAMS,
                message="'tool' is required in params",
                request_id=request.id,
            )

        # 通过 Server 创建异步任务
        result_str = await self._server.call_tool_async(
            tool_name, arguments, tenant_id=tenant_id,
        )

        try:
            result_data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            result_data = {"raw": result_str}

        return make_success_response(
            result=result_data,
            request_id=request.id,
        )

    async def _handle_tasks_get(
        self,
        request: JSONRPCRequest,
    ) -> JSONRPCResponse:
        """处理 tasks/get 请求 — 查询任务状态。

        请求 params 格式::
            {"task_id": "abc123..."}
        """
        if not isinstance(request.params, dict):
            return make_error_response(
                code=INVALID_PARAMS,
                message="params must be a JSON object",
                request_id=request.id,
            )

        task_id = request.params.get("task_id", "")
        if not task_id:
            return make_error_response(
                code=INVALID_PARAMS,
                message="'task_id' is required in params",
                request_id=request.id,
            )

        task = await self._task_store.get_task(task_id)
        if task is None:
            return make_error_response(
                code=TASK_NOT_FOUND,
                message=f"Task not found: {task_id}",
                request_id=request.id,
            )

        # 构建响应
        result: dict[str, Any] = {
            "task_id": task.get("task_id", task_id),
            "status": task.get("status", "unknown"),
        }

        if task.get("status") in ("completed",):
            result["result"] = task.get("result")
        elif task.get("status") in ("failed",):
            result["error"] = task.get("error")

        if not self._task_store.is_terminal(task.get("status", "")):
            result["poll_interval_ms"] = self._task_store.poll_interval_ms

        return make_success_response(
            result=result,
            request_id=request.id,
        )

    async def _handle_tasks_cancel(
        self,
        request: JSONRPCRequest,
    ) -> JSONRPCResponse:
        """处理 tasks/cancel 请求 — 取消任务。

        请求 params 格式::
            {"task_id": "abc123..."}
        """
        if not isinstance(request.params, dict):
            return make_error_response(
                code=INVALID_PARAMS,
                message="params must be a JSON object",
                request_id=request.id,
            )

        task_id = request.params.get("task_id", "")
        if not task_id:
            return make_error_response(
                code=INVALID_PARAMS,
                message="'task_id' is required in params",
                request_id=request.id,
            )

        cancelled = await self._task_store.cancel_task(task_id)
        if not cancelled:
            # 检查任务是否存在
            task = await self._task_store.get_task(task_id)
            if task is None:
                return make_error_response(
                    code=TASK_NOT_FOUND,
                    message=f"Task not found: {task_id}",
                    request_id=request.id,
                )
            return make_error_response(
                code=TASK_ALREADY_TERMINAL,
                message=f"Task already in terminal state: {task.get('status')}",
                request_id=request.id,
            )

        return make_success_response(
            result={
                "task_id": task_id,
                "status": "cancelled",
            },
            request_id=request.id,
        )

    def _handle_notification(
        self,
        request: JSONRPCRequest,
    ) -> JSONRPCResponse:
        """处理通知请求 — 无响应体，但返回空响应对象以保持接口一致。

        MCP 2026-07-28 规范：notifications/initialized 表示客户端初始化完成，
        服务器无需响应。但 JSON-RPC 通知由 id==null 标识，仍然返回空响应。
        """
        log.info("mcp.notification.initialized")
        return make_notification_response()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_call_params(self, request: JSONRPCRequest) -> dict[str, Any]:
        """从请求中提取 tools/call 参数。

        Returns:
            {"name": "...", "arguments": {...}} 或空字典。
        """
        if isinstance(request.params, dict):
            return request.params
        return {}


# ======================================================================
# SSE 序列化工具
# ======================================================================


def sse_serialize(response: JSONRPCResponse) -> str:
    """将 JSON-RPC 响应序列化为 SSE 事件格式。

    SSE 格式::

        data: {"jsonrpc": "2.0", "result": {...}, "id": "1"}\n\n

    Args:
        response: JSON-RPC 响应对象。

    Returns:
        SSE 格式字符串（含双换行符结尾）。
    """
    return f"data: {response.to_json()}\n\n"


def sse_serialize_error(
    error: dict[str, Any],
    request_id: str | int | float | None = None,
) -> str:
    """将错误信息序列化为 SSE 事件格式。

    Args:
        error: 错误字典。
        request_id: 请求 ID。

    Returns:
        SSE 格式字符串。
    """
    response = make_error_response(
        code=error.get("code", INTERNAL_ERROR),
        message=error.get("message", "Unknown error"),
        data=error.get("data"),
        request_id=request_id,
    )
    return sse_serialize(response)