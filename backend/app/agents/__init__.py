"""
Agent 层统一导出 — 导入所有 Agent 类并触发注册。

遵循单一职责：本文件仅做导出与注册触发，不包含业务逻辑。

导入此模块即自动注册所有内置 Agent 类型到 AgentRegistry：
    qa       → QAAgent
    workflow → WorkflowAgent
    action   → ActionAgent

同时导出 CrewAI 多 Agent 协作组件与 MCP 工具封装：
    KnowledgeBaseCrew        → 复杂任务的多 Agent 协作编排
    MCPToolWrapper           → MCP 工具适配为 CrewAI BaseTool
    get_mcp_tools_for_crewai → 获取 CrewAI 适配的工具列表
    get_mcp_tools_for_llm    → 获取 LLM function-calling 格式工具列表
"""

from app.agents.action_agent import ActionAgent
from app.agents.base import AgentState, BaseAgent
from app.agents.crew import KnowledgeBaseCrew
from app.agents.mcp_tools import (
    MCPToolWrapper,
    get_mcp_tools_for_crewai,
    get_mcp_tools_for_llm,
)
from app.agents.qa_agent import QAAgent
from app.agents.registry import AgentRegistry
from app.agents.workflow_agent import WorkflowAgent

__all__ = [
    "BaseAgent",
    "QAAgent",
    "WorkflowAgent",
    "ActionAgent",
    "AgentRegistry",
    "AgentState",
    "KnowledgeBaseCrew",
    "MCPToolWrapper",
    "get_mcp_tools_for_crewai",
    "get_mcp_tools_for_llm",
]
