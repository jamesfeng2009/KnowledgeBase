"""MCP 工具封装 — 将 MCP 工具适配为 Agent 可用的统一接口。

职责：
1. 列出 MCP Server 暴露的所有工具
2. 将 MCP 工具适配为 CrewAI BaseTool 接口
3. 将 MCP 工具适配为 LLM function-calling 的 tools 格式

遵循单一职责：只做工具适配，不做工具实现（工具实现在 mcp/server.py）。
遵循开闭原则：新增 MCP 工具只需在 server.py 用 @mcp_tool 注册，本模块自动发现。
"""
from __future__ import annotations

import json
from typing import Any

from app.mcp.client import MCPClient
from app.utils.logger import get_logger

logger = get_logger(__name__)

# CrewAI BaseTool 延迟导入
try:
    from crewai.tools import BaseTool as CrewBaseTool
    CREWAI_AVAILABLE = True
except ImportError:
    CrewBaseTool = object  # 降级为 object 基类
    CREWAI_AVAILABLE = False


class MCPToolWrapper(CrewBaseTool):
    """将单个 MCP 工具适配为 CrewAI BaseTool。

    CrewAI Agent 通过此包装调用 MCP 工具，
    内部委托给 MCPClient.call_tool()。
    """

    name: str = ""
    description: str = ""

    def __init__(self, tool_name: str, tool_description: str, mcp_client: MCPClient):
        # CrewAI BaseTool 使用 Pydantic，需要通过 __init__ 设置属性
        super().__init__()
        self.name = tool_name
        self.description = tool_description
        self._mcp_client = mcp_client

    def _run(self, **kwargs: Any) -> str:
        """CrewAI 同步调用入口（内部转异步）。"""
        import asyncio

        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 如果已在异步上下文中，创建 task
            future = asyncio.ensure_future(self._arun(**kwargs))
            return loop.run_until_complete(future)
        return loop.run_until_complete(self._arun(**kwargs))

    async def _arun(self, **kwargs: Any) -> str:
        """CrewAI 异步调用入口。"""
        try:
            result = await self._mcp_client.call_tool(self.name, kwargs)
            return result
        except Exception as e:
            logger.error("mcp_tool_wrapper.error", tool=self.name, error=str(e))
            return json.dumps({"error": str(e)}, ensure_ascii=False)


async def get_mcp_tools_for_crewai(mcp_client: MCPClient) -> list[MCPToolWrapper]:
    """获取所有 MCP 工具的 CrewAI 适配列表。

    通过 MCPClient.get_tools_for_llm() 获取工具定义（list[Tool]），
    每个 Tool 含 name / description / parameters 字段，包装为 MCPToolWrapper。

    Args:
        mcp_client: MCP 客户端实例。

    Returns:
        MCPToolWrapper 列表（空列表表示无可用工具或 CrewAI 未安装）。
    """
    if not CREWAI_AVAILABLE:
        logger.warning("mcp_tools.crewai_not_available")
        return []

    try:
        # 从 MCP Client 获取工具列表（Tool TypedDict，含 name/description/parameters）
        tools_info = await mcp_client.get_tools_for_llm()
        wrappers = []
        for tool_info in tools_info:
            wrapper = MCPToolWrapper(
                tool_name=tool_info.get("name", ""),
                tool_description=tool_info.get("description", ""),
                mcp_client=mcp_client,
            )
            wrappers.append(wrapper)
        logger.info("mcp_tools.loaded", count=len(wrappers))
        return wrappers
    except Exception as e:
        logger.error("mcp_tools.load_error", error=str(e))
        return []


async def get_mcp_tools_for_llm(mcp_client: MCPClient) -> list[dict[str, Any]]:
    """获取 MCP 工具的 LLM function-calling 格式。

    将 MCPClient 的 Tool 定义（parameters 字段）转换为 Anthropic 原生的
    input_schema 格式，兼容 Anthropic 和 OpenAI 的 tools 参数：

    [
        {
            "name": "knowledge_search",
            "description": "搜索企业知识库",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"]
            }
        }
    ]

    Args:
        mcp_client: MCP 客户端实例。

    Returns:
        工具定义列表（LLM function-calling 格式）。
    """
    try:
        tools_info = await mcp_client.get_tools_for_llm()
        return [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                # Tool.parameters 映射为 Anthropic 的 input_schema
                "input_schema": t.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
            for t in tools_info
        ]
    except Exception as e:
        logger.error("mcp_tools.llm_format_error", error=str(e))
        return []
