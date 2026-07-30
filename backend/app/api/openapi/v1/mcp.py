"""MCP 工具协议开放 API — 暴露 MCP 工具给外部 AI Agent。

将内部 MCP Server 注册的工具以标准 HTTP 接口暴露给外部 AI Agent，
使其无需接入 MCP 协议即可调用知识库工具。

对齐 MCP 2026-07-28 规范 Tasks 扩展核心语义：
- 长耗时工具（``long_running=True``）返回持久化 taskId 句柄
- 客户端通过 ``GET /mcp/tasks/{task_id}`` 轮询任务状态
- 支持 ``POST /mcp/tasks/{task_id}/cancel`` 协作式取消

权限说明：
- 需要 scope: ``mcp:use``；
- 认证方式为 API Key（X-API-Key header）。

遵循单一职责：本模块仅做 HTTP 路由与工具调用转发，
工具实现逻辑由 KnowledgeBaseMCPServer 提供。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.openapi.deps import require_scope
from app.database import async_session_factory
from app.mcp.client import MCPClient
from app.mcp.server import KnowledgeBaseMCPServer
from app.mcp.task_store import get_task_store
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
    mode: Literal["auto", "sync", "async"] = Field(
        default="auto",
        description=(
            "调用模式："
            "auto=根据工具 long_running 标记自动选择（默认）；"
            "sync=强制同步调用（阻塞等待结果）；"
            "async=强制异步调用（返回 taskId 句柄）。"
        ),
    )


# ======================================================================
# 端点 — 工具列表与 Schema
# ======================================================================


@router.get("/tools", response_model=ApiResponse[list[dict]])
async def list_tools(
    api_key_info: dict = Depends(require_scope("mcp:use")),
) -> ApiResponse[list[dict]]:
    """列出可用的 MCP 工具。

    返回工具名称、描述、入参 JSON Schema 与 long_running 标记，
    供外部 Agent 决策调用模式（同步/异步）。
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
                "long_running": client.is_long_running(t.get("name", "")),
            }
            for t in tools
        ],
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
                    "long_running": client.is_long_running(tool_name),
                },
                message="success",
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"工具 {tool_name} 不存在",
    )


# ======================================================================
# 端点 — 工具调用（支持同步/异步模式）
# ======================================================================


@router.post("/tools/{tool_name}/invoke", response_model=ApiResponse[dict])
async def invoke_tool(
    tool_name: str,
    body: InvokeToolRequest,
    api_key_info: dict = Depends(require_scope("mcp:use")),
) -> ApiResponse[dict]:
    """调用指定的 MCP 工具。

    调用模式由 ``body.mode`` 控制：
    - ``auto``（默认）：工具标记为 ``long_running`` 时走异步，否则同步
    - ``sync``：强制同步阻塞，等待结果返回
    - ``async``：强制异步，返回 taskId 句柄

    异步模式下返回结构::

        {
            "tool": "tool_name",
            "result": {
                "task_id": "abc123...",
                "status": "working",
                "poll_interval_ms": 2000,
                "ttl_ms": 3600000
            }
        }

    客户端凭 ``task_id`` 轮询 ``GET /mcp/tasks/{task_id}`` 获取最终结果。

    Args:
        tool_name: 工具名称。
        body: 工具入参与调用模式。

    Raises:
        HTTPException 502: 工具调用失败。
    """
    client = _get_mcp_client()
    logger.info(
        "openapi.mcp.invoke",
        tool=tool_name,
        mode=body.mode,
        key_name=api_key_info.get("name"),
    )

    # 判断是否走异步任务模式
    use_async = body.mode == "async" or (
        body.mode == "auto" and client.is_long_running(tool_name)
    )

    try:
        if use_async:
            # 异步模式 — 返回 taskId 句柄，不阻塞 HTTP 连接
            result_str = await client.call_tool_async(
                tool_name,
                body.arguments,
                tenant_id=api_key_info.get("tenant_id"),
            )
        else:
            # 同步模式 — 阻塞等待结果
            result_str = await client.call_tool(
                tool_name,
                body.arguments,
                tenant_id=api_key_info.get("tenant_id"),
            )
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


# ======================================================================
# 端点 — 任务轮询与取消（对齐 MCP Tasks 扩展）
# ======================================================================


@router.get("/tasks/{task_id}", response_model=ApiResponse[dict])
async def get_task(
    task_id: str,
    api_key_info: dict = Depends(require_scope("mcp:use")),
) -> ApiResponse[dict]:
    """查询任务状态 — 客户端轮询此端点获取长耗时任务的执行结果。

    对齐 MCP 2026-07-28 Tasks 扩展 ``tasks/get`` 语义：
    - ``working``：任务执行中，继续轮询
    - ``completed``：任务完成，``result`` 字段包含最终输出
    - ``failed``：任务失败，``error`` 字段包含错误信息
    - ``cancelled``：任务已取消

    任务状态持久化在 Redis 中，即使 API Server 重启或换实例，
    凭 ``task_id`` 仍可查询（在 TTL 有效期内）。

    Args:
        task_id: 任务 ID（由 invoke_tool 异步模式返回）。

    Raises:
        HTTPException 404: 任务不存在或已过期。
    """
    store = get_task_store()
    task = await store.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务不存在或已过期: {task_id}",
        )

    # 构建响应 — 终态包含 result / error，非终态仅返回 status
    response_data: dict[str, Any] = {
        "task_id": task.get("task_id", task_id),
        "tool": task.get("tool", ""),
        "status": task.get("status", "unknown"),
    }

    # 仅在终态时返回 result / error（减少非终态轮询的 payload）
    if task.get("status") == "completed":
        response_data["result"] = task.get("result")
    elif task.get("status") == "failed":
        response_data["error"] = task.get("error")

    # 附带轮询建议（非终态时）
    if not store.is_terminal(task.get("status", "")):
        response_data["poll_interval_ms"] = store.poll_interval_ms

    return ApiResponse(
        code=0,
        data=response_data,
        message="success",
    )


@router.post("/tasks/{task_id}/cancel", response_model=ApiResponse[dict])
async def cancel_task(
    task_id: str,
    api_key_info: dict = Depends(require_scope("mcp:use")),
) -> ApiResponse[dict]:
    """取消任务 — 协作式取消，标记状态为 cancelled。

    对齐 MCP 2026-07-28 Tasks 扩展 ``tasks/cancel`` 语义：
    取消是协作式的 — 服务器确认取消意图，但不保证立即停止执行。
    任务可能仍会到达 ``completed`` 或 ``failed`` 终态。

    Args:
        task_id: 任务 ID。

    Raises:
        HTTPException 404: 任务不存在或已过期。
        HTTPException 409: 任务已处于终态，无法取消。
    """
    store = get_task_store()
    cancelled = await store.cancel_task(task_id)
    if not cancelled:
        # 区分 "不存在" 和 "已终态"
        task = await store.get_task(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在或已过期: {task_id}",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务已处于终态（{task.get('status')}），无法取消",
        )

    logger.info("openapi.mcp.task_cancelled", task_id=task_id)
    return ApiResponse(
        code=0,
        data={"task_id": task_id, "status": "cancelled"},
        message="success",
    )
