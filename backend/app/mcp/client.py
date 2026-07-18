"""
MCP Client — 单一职责：Agent Loop 调用 MCP 工具的统一入口。

作为 Agent Loop 与 MCP Server 之间的适配层，将 LLM 的 ToolUse 格式
转换为 Server 的调用接口，屏蔽底层工具分发细节。

遵循单一职责：MCPClient 只负责工具列表获取与调用转发，
不包含工具实现逻辑（工具实现由 KnowledgeBaseMCPServer 提供）。
遵循依赖倒置：Agent Loop 依赖 MCPClient，不直接操作 Server。
"""

from __future__ import annotations

from app.llm.base import Tool, ToolUse
from app.mcp.server import KnowledgeBaseMCPServer
from app.utils.logger import get_logger

log = get_logger(__name__)


class MCPClient:
    """MCP Client — Agent Loop 通过此客户端调用 MCP 工具。

    将 LLM 的 ``ToolUse``（type/id/name/input）转换为 Server 的
    ``call_tool(name, arguments)`` 调用，是 Agent Loop 最常用的入口。

    使用方式::

        from app.mcp.server import KnowledgeBaseMCPServer
        from app.mcp.client import MCPClient

        server = KnowledgeBaseMCPServer(db_factory=async_session_factory)
        client = MCPClient(server)

        # 1. 获取工具列表传给 LLM
        tools = await client.get_tools_for_llm()

        # 2. LLM 返回 tool_use 后，直接调用
        result = await client.call_tool_from_llm(tool_use)
    """

    def __init__(self, server: KnowledgeBaseMCPServer) -> None:
        """初始化 MCP Client。

        Args:
            server: MCP Server 实例，提供工具注册与分发能力。
        """
        self._server = server

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

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用指定工具。

        Args:
            tool_name: 工具名称。
            arguments: 工具入参字典。

        Returns:
            工具执行结果（JSON 序列化字符串）。
        """
        return await self._server.call_tool(tool_name, arguments)

    async def call_tool_from_llm(self, tool_use: ToolUse) -> str:
        """从 LLM 返回的 ``ToolUse`` 直接调用工具。

        将 LLM 的 ``ToolUse``（type/id/name/input）解包为
        ``call_tool(name, arguments)``，是 Agent Loop 最常用的入口。

        Args:
            tool_use: LLM 返回的工具调用请求，包含 name 和 input 字段。

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
        return await self._server.call_tool(tool_name, arguments)
