"""
JSON-RPC 2.0 协议层 — 对齐 MCP 2026-07-28 StreamableHTTP 规范。

严格遵循 JSON-RPC 2.0 规范（https://www.jsonrpc.org/specification）：

- 请求：method + params + id（id 为 null 时视为通知，不返回响应）
- 响应：result 或 error，与请求的 id 对应
- 错误：标准 JSON-RPC 错误码 + MCP 扩展错误码

MCP 2026-07-28 StreamableHTTP 协议方法映射:

| JSON-RPC Method                        | 方向         | 说明                     |
|----------------------------------------|-------------|--------------------------|
| tools/list                             | 请求→响应    | 列出可用工具               |
| tools/call                             | 请求→响应    | 调用工具（同步或异步）       |
| resources/list                         | 请求→响应    | 列出可用资源（含元数据）     |
| resources/read                         | 请求→响应    | 读取指定资源内容            |
| tasks/create                           | 请求→响应    | 创建长耗时任务              |
| tasks/get                              | 请求→响应    | 查询任务状态               |
| tasks/cancel                           | 请求→响应    | 取消任务                   |
| notifications/initialized              | 通知        | 客户端初始化完成通知         |

StreamableHTTP 传输约定：
- 请求 Content-Type: application/json
- 同步响应 Content-Type: application/json
- 流式响应 Content-Type: text/event-stream（SSE 格式逐条推送 JSON-RPC 响应）
- 每个 HTTP 请求可以包含一个 JSON-RPC 请求

遵循单一职责：本模块只定义协议数据模型和编解码，不涉及传输逻辑。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, fields
from typing import Any, Generic, Literal, TypeVar

# ======================================================================
# JSON-RPC 2.0 规范错误码
# ======================================================================

#: 标准 JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: MCP 扩展错误码（-32000 至 -32099 范围）
TOOL_NOT_FOUND = -32000
TOOL_EXECUTION_ERROR = -32001
TASK_NOT_FOUND = -32002
TASK_ALREADY_TERMINAL = -32003
AUTHENTICATION_ERROR = -32004
RESOURCE_NOT_FOUND = -32005

#: 错误消息映射
_ERROR_MESSAGES: dict[int, str] = {
    PARSE_ERROR: "Parse error",
    INVALID_REQUEST: "Invalid request",
    METHOD_NOT_FOUND: "Method not found",
    INVALID_PARAMS: "Invalid params",
    INTERNAL_ERROR: "Internal error",
    TOOL_NOT_FOUND: "Tool not found",
    TOOL_EXECUTION_ERROR: "Tool execution error",
    TASK_NOT_FOUND: "Task not found",
    TASK_ALREADY_TERMINAL: "Task already in terminal state",
    AUTHENTICATION_ERROR: "Authentication error",
    RESOURCE_NOT_FOUND: "Resource not found",
}


# ======================================================================
# 协议数据模型
# ======================================================================


@dataclass
class JSONRPCRequest:
    """JSON-RPC 2.0 请求对象。

    Attributes:
        jsonrpc: 固定为 "2.0"。
        method: 要调用的方法名。
        params: 方法参数（可选，可为 dict 或 list）。
        id: 请求标识符（数字或字符串）。
            id 为 None 时视为通知（Notification），不返回响应。
    """

    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] | list[Any] | None = None
    id: str | int | float | None = None

    def is_notification(self) -> bool:
        """是否为通知（无需响应）。"""
        return self.id is None


class JSONRPCError(Exception):
    """JSON-RPC 2.0 错误对象（同时也是异常，可被 except 捕获）。

    Attributes:
        code: 错误码（整数）。
        message: 错误消息（简短字符串）。
        data: 附加错误数据（可选，可用于传递调试信息）。
    """

    def __init__(
        self,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.data is not None:
            result["data"] = self.data
        return result


T = TypeVar("T")


@dataclass
class JSONRPCResponse(Generic[T]):
    """JSON-RPC 2.0 响应对象。

    Attributes:
        jsonrpc: 固定为 "2.0"。
        result: 请求结果（成功时）。
        error: 错误对象（失败时）。
        id: 对应的请求标识符。
    """

    jsonrpc: str = "2.0"
    result: T | None = None
    error: JSONRPCError | None = None
    id: str | int | float | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-RPC 2.0 响应字典。

        始终包含 ``id`` 键（为 None 时输出 null）— JSON-RPC 2.0 规范要求
        parse error 等无法确定请求 id 的响应也必须显式携带 ``"id": null``。
        """
        result: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
        }
        if self.error is not None:
            result["error"] = self.error.to_dict()
        else:
            result["result"] = self.result
        result["id"] = self.id
        return result

    def to_json(self, ensure_ascii: bool = False) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(self.to_dict(), ensure_ascii=ensure_ascii, default=str)


# ======================================================================
# MCP StreamableHTTP 协议方法常量
# ======================================================================

#: 工具方法
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"

#: 资源方法
METHOD_RESOURCES_LIST = "resources/list"
METHOD_RESOURCES_READ = "resources/read"

#: 任务方法
METHOD_TASKS_CREATE = "tasks/create"
METHOD_TASKS_GET = "tasks/get"
METHOD_TASKS_CANCEL = "tasks/cancel"

#: 通知方法
METHOD_NOTIFICATION_INITIALIZED = "notifications/initialized"

#: 所有支持的 MCP 方法列表
SUPPORTED_MCP_METHODS: frozenset[str] = frozenset({
    METHOD_TOOLS_LIST,
    METHOD_TOOLS_CALL,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_TASKS_CREATE,
    METHOD_TASKS_GET,
    METHOD_TASKS_CANCEL,
    METHOD_NOTIFICATION_INITIALIZED,
})


# ======================================================================
# 协议编解码
# ======================================================================


def parse_request(raw: str | bytes | dict[str, Any]) -> JSONRPCRequest:
    """将原始 JSON 字符串或字典解析为 JSONRPCRequest。

    Args:
        raw: 原始 JSON 字符串、字节或已解析的字典。

    Returns:
        解析后的 JSONRPCRequest。

    Raises:
        JSONRPCError: 解析失败或请求格式无效时抛出对应错误。
    """
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JSONRPCError(
                code=PARSE_ERROR,
                message="Parse error",
                data=f"Invalid JSON: {exc}",
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise JSONRPCError(
            code=INVALID_REQUEST,
            message="Invalid request",
            data="Request must be a JSON object",
        )

    # 校验 jsonrpc 版本
    if data.get("jsonrpc") != "2.0":
        raise JSONRPCError(
            code=INVALID_REQUEST,
            message="Invalid request",
            data='jsonrpc must be "2.0"',
        )

    method = data.get("method", "")
    if not isinstance(method, str) or not method:
        raise JSONRPCError(
            code=INVALID_REQUEST,
            message="Invalid request",
            data="method must be a non-empty string",
        )

    params = data.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        raise JSONRPCError(
            code=INVALID_REQUEST,
            message="Invalid request",
            data="params must be a JSON object or array",
        )

    req_id = data.get("id")
    if req_id is not None:
        # Python 中 bool 是 int 的子类，须明确排除
        if isinstance(req_id, bool) or not isinstance(req_id, (str, int, float)):
            raise JSONRPCError(
                code=INVALID_REQUEST,
                message="Invalid request",
                data="id must be a string, number, or null",
            )

    return JSONRPCRequest(
        jsonrpc="2.0",
        method=method,
        params=params,
        id=req_id,
    )


def make_success_response(
    result: Any,
    request_id: str | int | float | None = None,
) -> JSONRPCResponse:
    """构造成功响应。

    Args:
        result: 成功结果。
        request_id: 对应的请求 ID（通知时为 None）。

    Returns:
        JSONRPCResponse 对象。
    """
    return JSONRPCResponse(
        jsonrpc="2.0",
        result=result,
        error=None,
        id=request_id,
    )


def make_error_response(
    code: int,
    message: str | None = None,
    data: Any = None,
    request_id: str | int | float | None = None,
) -> JSONRPCResponse:
    """构造错误响应。

    Args:
        code: 错误码。
        message: 错误消息（为空时从错误码自动推导）。
        data: 附加错误数据。
        request_id: 对应的请求 ID。

    Returns:
        JSONRPCResponse 对象。
    """
    return JSONRPCResponse(
        jsonrpc="2.0",
        result=None,
        error=JSONRPCError(
            code=code,
            message=message or _ERROR_MESSAGES.get(code, "Unknown error"),
            data=data,
        ),
        id=request_id,
    )


def make_notification_response() -> JSONRPCResponse:
    """构造通知响应（id 为 None，表示无需返回）。"""
    return JSONRPCResponse(
        jsonrpc="2.0",
        result=None,
        error=None,
        id=None,
    )


# ======================================================================
# 工具方法请求/响应类型
# ======================================================================


def make_tools_list_params() -> dict[str, Any]:
    """构造 tools/list 请求参数。"""
    return {}


def make_tools_call_params(
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """构造 tools/call 请求参数。

    Args:
        name: 工具名称。
        arguments: 工具入参。

    Returns:
        符合 MCP 规范的请求参数字典。
    """
    return {
        "name": name,
        "arguments": arguments,
    }


# ======================================================================
# 资源方法请求/响应类型
# ======================================================================


def make_resources_list_params() -> dict[str, Any]:
    """构造 resources/list 请求参数。"""
    return {}


def make_resources_read_params(uri: str) -> dict[str, Any]:
    """构造 resources/read 请求参数。

    Args:
        uri: 资源 URI（如 ``resource://skill/knowledge_search``）。

    Returns:
        符合 MCP 规范的请求参数字典。
    """
    return {
        "uri": uri,
    }


def make_tasks_create_params(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """构造 tasks/create 请求参数。

    Args:
        tool_name: 工具名称。
        arguments: 工具入参。

    Returns:
        符合 MCP 规范的请求参数字典。
    """
    return {
        "tool": tool_name,
        "arguments": arguments,
    }


def make_tasks_get_params(task_id: str) -> dict[str, Any]:
    """构造 tasks/get 请求参数。

    Args:
        task_id: 任务 ID。

    Returns:
        符合 MCP 规范的请求参数字典。
    """
    return {
        "task_id": task_id,
    }


def make_tasks_cancel_params(task_id: str) -> dict[str, Any]:
    """构造 tasks/cancel 请求参数。

    Args:
        task_id: 任务 ID。

    Returns:
        符合 MCP 规范的请求参数字典。
    """
    return {
        "task_id": task_id,
    }