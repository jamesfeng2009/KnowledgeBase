"""
MCP 工具协议层 — 统一导出 MCP Server 与 Client。

遵循单一职责：本文件仅做导出，不包含业务逻辑。
遵循开闭原则：新增 MCP 工具只需在 server.py 中添加 ``@mcp_tool`` 方法，
无需修改本文件。

对外暴露 ``MCPServer`` 作为 ``KnowledgeBaseMCPServer`` 的语义别名，
调用方可按偏好使用任一名称。
"""

from app.mcp.client import MCPClient
from app.mcp.server import KnowledgeBaseMCPServer

# 语义别名 — 外部调用方可使用 MCPServer 或 KnowledgeBaseMCPServer
MCPServer = KnowledgeBaseMCPServer

__all__ = [
    "MCPServer",
    "KnowledgeBaseMCPServer",
    "MCPClient",
]
