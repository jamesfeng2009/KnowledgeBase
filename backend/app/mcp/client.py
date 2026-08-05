"""
MCP Client — 单一职责：Agent Loop 调用 MCP 工具的统一入口。

作为 Agent Loop 与 MCP Server 之间的适配层，将 LLM 的 ToolUse 格式
转换为 Server 的调用接口，屏蔽底层工具分发细节。

遵循单一职责：MCPClient 只负责工具列表获取与调用转发，
不包含工具实现逻辑（工具实现由 KnowledgeBaseMCPServer 提供）。
遵循依赖倒置：Agent Loop 依赖 MCPClient，不直接操作 Server。

StreamableHTTP 支持：
- ``jsonrpc_call`` / ``jsonrpc_tools_list`` / ``jsonrpc_tools_call``
  方法使用 JSON-RPC 2.0 协议格式通过 StreamableHTTPTransport 通信，
  对齐 MCP 2026-07-28 StreamableHTTP 规范。
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.base import Tool, ToolUse
from app.mcp.protocol import (
    JSONRPCResponse,
    make_resources_list_params,
    make_resources_read_params,
    make_success_response,
    make_tools_call_params,
    make_tools_list_params,
    parse_request,
)
from app.mcp.server import KnowledgeBaseMCPServer
from app.mcp.streamable_http import StreamableHTTPTransport
from app.utils.logger import get_logger

log = get_logger(__name__)


class MCPClient:
    """MCP Client — Agent Loop 通过此客户端调用 MCP 工具。

    将 LLM 的 ``ToolUse``（type/id/name/input）转换为 Server 的
    ``call_tool(name, arguments)`` 调用，是 Agent Loop 最常用的入口。

    StreamableHTTP 支持：
    通过 ``jsonrpc_*`` 方法家族使用 JSON-RPC 2.0 协议格式，
    与外部 HTTP 客户端使用相同的协议层。

    使用方式::

        from app.mcp.server import KnowledgeBaseMCPServer
        from app.mcp.client import MCPClient

        server = KnowledgeBaseMCPServer(db_factory=async_session_factory)
        client = MCPClient(server)

        # 1. 获取工具列表传给 LLM
        tools = await client.get_tools_for_llm()

        # 2. LLM 返回 tool_use 后，直接调用
        result = await client.call_tool_from_llm(tool_use)

        # 3. 使用 JSON-RPC 协议格式
        response = await client.jsonrpc_tools_list(request_id="1")
    """

    def __init__(self, server: KnowledgeBaseMCPServer) -> None:
        """初始化 MCP Client。

        Args:
            server: MCP Server 实例，提供工具注册与分发能力。
        """
        self._server = server
        # 用于 JSON-RPC 通信的传输层
        self._transport = StreamableHTTPTransport(server)

    async def get_tools_for_llm(self) -> list[Tool]:
        """返回 LLM 可用的工具列表。

        返回的 ``Tool`` 列表格式与 ``LLMProvider.chat(tools=...)`` 兼容，
        可直接传入（各 Provider 内部负责转换为 SDK 专属格式）。
        """
        return await self._server.list_tools()

    async def get_tools_by_names(self, names: list[str]) -> list[Tool]:
        """按名称子集返回工具列表 — Find Skills 按需加载入口。

        只有被 SkillFinder 匹配到的工具才会加载完整 schema，
        避免全量加载浪费 token。

        Args:
            names: 需要加载的工具名称列表。

        Returns:
            匹配到的 Tool 列表（可能为空）。
        """
        return await self._server.list_tools_by_names(names)

    def get_skill_index(self) -> list[dict]:
        """返回轻量技能索引 — 供 SkillFinder 意图匹配。

        索引仅含 name/category/tags/description，
        token 开销极小（每个技能约 20-30 token）。
        """
        return self._server.get_skill_index()

    # ------------------------------------------------------------------
    # 资源发现与读取（resources/list + resources/read）
    # ------------------------------------------------------------------

    async def list_resources(self) -> list[dict[str, Any]]:
        """返回资源列表 — 每个工具即一个资源，携带 Resource Metadata。

        元数据包含 domain / tags / when_to_use / when_not_to_use /
        output_interpretation / version / review_status，
        供 Agent 在调用前理解工具边界，减少误调用。
        """
        return await self._server.list_resources()

    async def read_resource(self, uri: str) -> dict[str, Any] | None:
        """读取单个资源完整内容。

        Args:
            uri: 资源 URI（``resource://skill/{tool_name}``）。

        Returns:
            资源字典（含 content）；URI 非法或资源不存在时返回 None。
        """
        return await self._server.read_resource(uri)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        *,
        tenant_id: str | None = None,
    ) -> str:
        """调用指定工具（同步阻塞模式 — Agent Loop 使用）。

        Args:
            tool_name: 工具名称。
            arguments: 工具入参字典。
            tenant_id: 请求级租户 ID（透传 Server 做租户过滤）。

        Returns:
            工具执行结果（JSON 序列化字符串）。
        """
        return await self._server.call_tool(tool_name, arguments, tenant_id=tenant_id)

    async def call_tool_async(
        self,
        tool_name: str,
        arguments: dict,
        *,
        tenant_id: str | None = None,
    ) -> str:
        """异步调用长耗时工具 — 返回任务句柄（HTTP API 层使用）。

        对齐 MCP 2026-07-28 Tasks 扩展：返回 taskId 而非阻塞等待。
        Agent Loop 通常不需要此方法（内部走同步 ``call_tool``）。

        Args:
            tool_name: 工具名称。
            arguments: 工具入参字典。
            tenant_id: 请求级租户 ID。

        Returns:
            JSON 字符串，包含 task_id / status / poll_interval_ms / ttl_ms。
        """
        return await self._server.call_tool_async(
            tool_name, arguments, tenant_id=tenant_id,
        )

    def is_long_running(self, tool_name: str) -> bool:
        """查询工具是否标记为长耗时。"""
        return self._server.is_long_running(tool_name)

    # ------------------------------------------------------------------
    # JSON-RPC 协议方法（对齐 MCP 2026-07-28 StreamableHTTP 规范）
    # ------------------------------------------------------------------

    async def jsonrpc_call(
        self,
        method: str,
        params: dict[str, Any] | list[Any] | None = None,
        request_id: str | int | float | None = None,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse:
        """通过 JSON-RPC 协议调用任意 MCP 方法。

        Args:
            method: JSON-RPC 方法名（如 "tools/list", "tools/call"）。
            params: 方法参数。
            request_id: 请求 ID（None 时视为通知）。
            tenant_id: 租户 ID。

        Returns:
            JSON-RPC 响应对象。
        """
        raw_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": request_id,
            },
            ensure_ascii=False,
        )
        result = await self._transport.handle_request(
            raw_body, tenant_id=tenant_id,
        )

        # 如果是 AsyncIterator（SSE 流式），迭代到最后一个响应
        if hasattr(result, "__aiter__"):
            final_response = None
            async for response in result:  # type: ignore[union-attr]
                final_response = response
            return final_response or make_success_response(
                result={"error": "No response from stream"},
                request_id=request_id,
            )

        return result  # type: ignore[return-value]

    async def jsonrpc_tools_list(
        self,
        request_id: str | int | float | None = None,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse:
        """通过 JSON-RPC 列出工具列表。

        Args:
            request_id: 请求 ID。
            tenant_id: 租户 ID。

        Returns:
            JSON-RPC 响应，result.tools 包含工具列表。
        """
        return await self.jsonrpc_call(
            method="tools/list",
            params=make_tools_list_params(),
            request_id=request_id,
            tenant_id=tenant_id,
        )

    async def jsonrpc_tools_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        request_id: str | int | float | None = None,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse:
        """通过 JSON-RPC 调用工具。

        Args:
            tool_name: 工具名称。
            arguments: 工具入参。
            request_id: 请求 ID。
            tenant_id: 租户 ID。

        Returns:
            JSON-RPC 响应，result.content 包含工具执行结果。
            对于长耗时工具，返回 result 包含 task_id/status。
        """
        return await self.jsonrpc_call(
            method="tools/call",
            params=make_tools_call_params(tool_name, arguments),
            request_id=request_id,
            tenant_id=tenant_id,
        )

    async def jsonrpc_tasks_get(
        self,
        task_id: str,
        request_id: str | int | float | None = None,
    ) -> JSONRPCResponse:
        """通过 JSON-RPC 查询任务状态。

        Args:
            task_id: 任务 ID。
            request_id: 请求 ID。

        Returns:
            JSON-RPC 响应，包含任务状态和结果。
        """
        return await self.jsonrpc_call(
            method="tasks/get",
            params={"task_id": task_id},
            request_id=request_id,
        )

    async def jsonrpc_resources_list(
        self,
        request_id: str | int | float | None = None,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse:
        """通过 JSON-RPC 列出资源列表（含 Resource Metadata）。

        Args:
            request_id: 请求 ID。
            tenant_id: 租户 ID。

        Returns:
            JSON-RPC 响应，result.resources 包含资源列表。
        """
        return await self.jsonrpc_call(
            method="resources/list",
            params=make_resources_list_params(),
            request_id=request_id,
            tenant_id=tenant_id,
        )

    async def jsonrpc_resources_read(
        self,
        uri: str,
        request_id: str | int | float | None = None,
        *,
        tenant_id: str | None = None,
    ) -> JSONRPCResponse:
        """通过 JSON-RPC 读取资源内容。

        Args:
            uri: 资源 URI（``resource://skill/{tool_name}``）。
            request_id: 请求 ID。
            tenant_id: 租户 ID。

        Returns:
            JSON-RPC 响应，result.contents 包含资源内容；
            资源不存在时返回 RESOURCE_NOT_FOUND 错误。
        """
        return await self.jsonrpc_call(
            method="resources/read",
            params=make_resources_read_params(uri),
            request_id=request_id,
            tenant_id=tenant_id,
        )

    async def jsonrpc_tasks_cancel(
        self,
        task_id: str,
        request_id: str | int | float | None = None,
    ) -> JSONRPCResponse:
        """通过 JSON-RPC 取消任务。

        Args:
            task_id: 任务 ID。
            request_id: 请求 ID。

        Returns:
            JSON-RPC 响应，包含取消结果。
        """
        return await self.jsonrpc_call(
            method="tasks/cancel",
            params={"task_id": task_id},
            request_id=request_id,
        )

    async def call_tool_from_llm(
        self,
        tool_use: ToolUse,
        *,
        tenant_id: str | None = None,
    ) -> str:
        """从 LLM 返回的 ``ToolUse`` 直接调用工具。

        将 LLM 的 ``ToolUse``（type/id/name/input）解包为
        ``call_tool(name, arguments)``，是 Agent Loop 最常用的入口。

        Args:
            tool_use: LLM 返回的工具调用请求，包含 name 和 input 字段。
            tenant_id: 请求级租户 ID（透传 Server 做租户过滤）。
                多租户场景必传 — 缺失时工具内查询不做租户过滤，
                存在跨租户数据泄漏风险。**不信任** LLM 在 input 中
                自封的租户标识，租户上下文必须由调用方从请求注入。

        Returns:
            工具执行结果（JSON 序列化字符串）。
        """
        tool_name = tool_use["name"]
        arguments = tool_use.get("input", {})
        tool_use_id = tool_use.get("id", "")
        log.info(
            "mcp.client.tool_use",
            tool=tool_name,
            tool_use_id=tool_use_id,
        )
        return await self._server.call_tool(tool_name, arguments, tenant_id=tenant_id)
