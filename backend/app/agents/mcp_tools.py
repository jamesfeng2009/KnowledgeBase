"""MCP 工具封装 — 将 MCP 工具适配为 Agent 可用的统一接口。

职责：
1. 列出 MCP Server 暴露的所有工具
2. 将 MCP 工具适配为 CrewAI BaseTool 接口
3. 将 MCP 工具适配为 LLM function-calling 的 tools 格式
4. P1: 按 Agent 类型筛选工具（QAAgent 只拿只读工具，ActionAgent 才拿写操作工具）

遵循单一职责：只做工具适配，不做工具实现（工具实现在 mcp/server.py）。
遵循开闭原则：新增 MCP 工具只需在 server.py 用 @mcp_tool 注册，本模块自动发现。

P1 工具分层设计（常驻工具 vs 长尾工具）：
    - 只读工具（knowledge_search / document_get / query_oa_approval）：
      QA / Workflow / Action 三类 Agent 均可使用。
    - 写操作工具（document_create / create_it_ticket）：
      仅 Action Agent 可用，QA Agent 只读不写。
    - 随工具增长，可进一步按 category 做细粒度分组。
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

# P1: 高风险/写操作工具集 — 仅 Action Agent 可用，ReviewerAgent 同步引用。
# 单一来源：新增高风险工具只需在此注册，agent 权限过滤与审查自动同步。
_HIGH_RISK_TOOLS: set[str] = {
    "document_create",
    "document_delete",
    "create_it_ticket",
    "system_config_change",
}

# 向后兼容别名
_WRITE_TOOLS: set[str] = _HIGH_RISK_TOOLS

# P1: 常驻只读工具 — 所有 Agent 类型均可使用
_READ_ONLY_TOOLS: set[str] = {
    "knowledge_search",
    "document_get",
    "query_oa_approval",
}


class MCPToolWrapper(CrewBaseTool):
    """将单个 MCP 工具适配为 CrewAI BaseTool。

    CrewAI Agent 通过此包装调用 MCP 工具，
    内部委托给 MCPClient.call_tool()。
    """

    name: str = ""
    description: str = ""

    def __init__(
        self,
        tool_name: str,
        tool_description: str,
        mcp_client: MCPClient,
        tenant_id: str | None = None,
    ):
        # CrewAI BaseTool 使用 Pydantic，需要通过 __init__ 设置属性
        super().__init__()
        self.name = tool_name
        self.description = tool_description
        self._mcp_client = mcp_client
        self._tenant_id = tenant_id

    def _run(self, **kwargs: Any) -> str:
        """CrewAI 同步调用入口（内部转异步）。

        注意：不能对运行中的事件循环调用 run_until_complete（RuntimeError）。
        已处于异步上下文时，改为在独立线程的新事件循环中执行并同步等待。
        """
        import asyncio
        import concurrent.futures

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中的事件循环 — 直接同步驱动
            return asyncio.run(self._arun(**kwargs))
        # 已在异步上下文中 — 在独立线程的新事件循环中执行，避免阻塞当前循环
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, self._arun(**kwargs)).result()

    async def _arun(self, **kwargs: Any) -> str:
        """CrewAI 异步调用入口。"""
        try:
            result = await self._mcp_client.call_tool(
                self.name, kwargs, tenant_id=self._tenant_id
            )
            return result
        except Exception as e:
            logger.error("mcp_tool_wrapper.error", tool=self.name, error=str(e))
            return json.dumps({"error": str(e)}, ensure_ascii=False)


async def get_mcp_tools_for_crewai(
    mcp_client: MCPClient,
    tenant_id: str | None = None,
) -> list[MCPToolWrapper]:
    """获取所有 MCP 工具的 CrewAI 适配列表。

    通过 MCPClient.get_tools_for_llm() 获取工具定义（list[Tool]），
    每个 Tool 含 name / description / parameters 字段，包装为 MCPToolWrapper。

    .. deprecated::
        此函数全量加载所有工具。P1 改用 ``get_mcp_tools_for_agent_type``
        按 Agent 类型筛选，避免 QA Agent 拿到写操作工具。

    Args:
        mcp_client: MCP 客户端实例。
        tenant_id: 租户 ID，透传给 MCP 工具调用。

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
                tenant_id=tenant_id,
            )
            wrappers.append(wrapper)
        logger.info("mcp_tools.loaded", count=len(wrappers))
        return wrappers
    except Exception as e:
        logger.error("mcp_tools.load_error", error=str(e))
        return []


async def get_mcp_tools_for_agent_type(
    mcp_client: MCPClient,
    agent_type: str,
    tenant_id: str | None = None,
) -> list[MCPToolWrapper]:
    """按 Agent 类型筛选工具 — P1 工具分层注入。

    设计原理：QA Agent 只负责回答问题，不应有写操作权限；
    Action Agent 负责执行操作，可以拿到写工具。这避免了 LLM
    在 QA 场景误调用 document_create / create_it_ticket 的问题。

    分层规则::

        agent_type="qa"       → 只读工具（排除写操作）
        agent_type="workflow" → 只读工具 + 工作流查询（排除写操作）
        agent_type="action"   → 全部工具（含写操作）
        其他                   → 只读工具（安全默认）

    Args:
        mcp_client: MCP 客户端实例。
        agent_type: Agent 类型标识（qa / workflow / action）。
        tenant_id: 租户 ID，透传给 MCP 工具调用。

    Returns:
        筛选后的 MCPToolWrapper 列表。
    """
    if not CREWAI_AVAILABLE:
        logger.warning("mcp_tools.crewai_not_available")
        return []

    try:
        tools_info = await mcp_client.get_tools_for_llm()

        # Action Agent 拿全部工具
        if agent_type == "action":
            allowed_names = {t.get("name", "") for t in tools_info}
        else:
            # QA / Workflow / 未知类型 → 只读工具（排除高风险/写操作）
            allowed_names = _READ_ONLY_TOOLS

        wrappers = []
        for tool_info in tools_info:
            tool_name = tool_info.get("name", "")
            if tool_name not in allowed_names:
                continue
            wrapper = MCPToolWrapper(
                tool_name=tool_name,
                tool_description=tool_info.get("description", ""),
                mcp_client=mcp_client,
                tenant_id=tenant_id,
            )
            wrappers.append(wrapper)

        logger.info(
            "mcp_tools.filtered_for_agent",
            agent_type=agent_type,
            total=len(tools_info),
            filtered=len(wrappers),
            tools=[w.name for w in wrappers],
        )
        return wrappers
    except Exception as e:
        logger.error("mcp_tools.filter_error", error=str(e), agent_type=agent_type)
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
