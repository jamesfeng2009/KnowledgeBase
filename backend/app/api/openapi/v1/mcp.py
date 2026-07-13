"""MCP 工具协议开放 API — 暴露 MCP 工具给外部 AI Agent。

将内部 MCP Server 注册的工具以标准 HTTP 接口暴露给外部 AI Agent，
使其无需接入 MCP 协议即可调用知识库工具。

权限说明：
- 需要 scope: ``mcp:use``；
- 认证方式为 API Key（X-API-Key header）。

遵循单一职责：本模块仅做 HTTP 路由与工具调用转发，
工具实现逻辑由 KnowledgeBaseMCPServer 提供。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.openapi.deps import require_scope
from app.database import async_session_factory
from app.mcp.client import MCPClient
from app.mcp.server import KnowledgeBaseMCPServer
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/mcp", tags=["开放接口-MCP 工具协议"])


def _get_mcp_client() -> MCPClient:
    """构造 MCP Client — 基于 async_session_factory 创建 Server。"""
    server = KnowledgeBaseMCPServer(db_factory=async_session_factory)
    return MCPClient(server)


# ======================================================================
# 请求 Schema
# ======================================================================


class InvokeToolRequest(BaseModel):
    """MCP 工具调用请求。"""

    arguments: dict[str, Any] = Field(
        default_factory=dict, description="工具入参"
    )


# ======================================================================
# 端点
# ======================================================================


@router.get("/tools", response_model=ApiResponse[list[dict]])
async def list_tools(
    api_key_info: dict = Depends(require_scope("mcp:use")),
) -> ApiResponse[list[dict]]:
    """列出可用的 MCP 工具。

    返回工具名称、描述与入参 JSON Schema，供外部 Agent 决策调用。
    """
    client = _get_mcp_client()
    try:
        tools = await client.get_tools_for_llm()
    except Exception as exc:
        logger.error("openapi.mcp.list_tools_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"获取工具列表失败: {exc}",
        ) from exc

    return ApiResponse(
        code=0,
        data=[
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
            }
            for t in tools
        ],
        message="success",
    )


@router.post("/tools/{tool_name}/invoke", response_model=ApiResponse[dict])
async def invoke_tool(
    tool_name: str,
    body: InvokeToolRequest,
    api_key_info: dict = Depends(require_scope("mcp:use")),
) -> ApiResponse[dict]:
    """调用指定的 MCP 工具。

    Args:
        tool_name: 工具名称。
        body: 工具入参。

    Raises:
        HTTPException 404: 工具不存在或调用失败。
    """
    client = _get_mcp_client()
    logger.info(
        "openapi.mcp.invoke",
        tool=tool_name,
        key_name=api_key_info.get("name"),
    )

    try:
        result_str = await client.call_tool(tool_name, body.arguments)
    except Exception as exc:
        logger.error("openapi.mcp.invoke_error", tool=tool_name, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"工具调用失败: {exc}",
        ) from exc

    # 尝试解析 JSON 结果，失败则返回原始字符串
    try:
        result_data: Any = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        result_data = result_str

    return ApiResponse(
        code=0,
        data={"tool": tool_name, "result": result_data},
        message="success",
    )


@router.get("/tools/{tool_name}/schema", response_model=ApiResponse[dict])
async def get_tool_schema(
    tool_name: str,
    api_key_info: dict = Depends(require_scope("mcp:use")),
) -> ApiResponse[dict]:
    """获取指定工具的 JSON Schema。

    Raises:
        HTTPException 404: 工具不存在。
    """
    client = _get_mcp_client()
    try:
        tools = await client.get_tools_for_llm()
    except Exception as exc:
        logger.error("openapi.mcp.schema_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"获取工具列表失败: {exc}",
        ) from exc

    for tool in tools:
        if tool.get("name") == tool_name:
            return ApiResponse(
                code=0,
                data={
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
                message="success",
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"工具 {tool_name} 不存在",
    )
