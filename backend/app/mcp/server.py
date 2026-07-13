"""
MCP Server — 单一职责：暴露知识库工具给 AI Agent。

采用进程内调用模式（不依赖外部 stdio/SSE 传输），Server 与 Client
在同一进程内直接通信，降低延迟。

遵循开闭原则：新增工具只需用 ``@mcp_tool`` 装饰一个新方法并实现逻辑，
``list_tools`` / ``call_tool`` 会自动发现并分发，无需修改任何既有代码。
遵循单一职责：KnowledgeBaseMCPServer 只负责工具注册与分发，
不包含业务逻辑（数据访问委托 Repository 层，企业系统对接委托外部 Client）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import Tool
from app.models.knowledge import Document
from app.repositories.knowledge_repository import (
    DocumentRepository,
    KnowledgeBaseRepository,
)
from app.utils.logger import get_logger

log = get_logger(__name__)


def mcp_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器：将方法注册为 MCP 工具。

    开闭原则落点：新增工具只需用此装饰器标注一个新的异步方法，
    ``_scan_tools`` 会自动收集，``list_tools`` / ``call_tool`` 自动分发，
    无需修改 ``KnowledgeBaseMCPServer`` 的任何既有代码。

    Args:
        name: 工具名称（对应 LLM function calling 的 function name）。
        description: 工具描述，供 LLM 决策调用。
        parameters: 工具入参 JSON Schema（``{"type": "object", "properties": ...}``）。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func._mcp_tool_name = name  # type: ignore[attr-defined]
        func._mcp_tool_description = description  # type: ignore[attr-defined]
        func._mcp_tool_parameters = parameters  # type: ignore[attr-defined]
        return func

    return decorator


class KnowledgeBaseMCPServer:
    """知识库 MCP Server — 暴露知识库能力给 AI Agent。

    通过 ``db_factory`` 获取数据库会话，每个工具方法独立管理会话生命周期
    （commit / rollback / close），互不干扰。

    使用方式::

        from app.database import async_session_factory

        server = KnowledgeBaseMCPServer(db_factory=async_session_factory)
        tools = await server.list_tools()
        result = await server.call_tool("knowledge_search", {"query": "报销流程"})
    """

    def __init__(self, db_factory: Callable[[], AsyncSession]) -> None:
        """初始化 MCP Server。

        Args:
            db_factory: 返回 ``AsyncSession`` 的可调用对象
                       （通常是 ``async_sessionmaker`` 实例）。
        """
        self._db_factory = db_factory
        # 工具注册表：tool_name -> {"handler": method, "definition": Tool}
        self._tool_registry: dict[str, dict[str, Any]] = {}
        self._scan_tools()

    # ------------------------------------------------------------------
    # 工具注册 — 自动扫描 @mcp_tool 装饰的方法
    # ------------------------------------------------------------------

    def _scan_tools(self) -> None:
        """扫描实例方法，收集所有 ``@mcp_tool`` 装饰的方法到注册表。

        在 ``__init__`` 中调用一次，后续 ``list_tools`` / ``call_tool``
        直接查表，避免每次调用都遍历 ``dir(self)``。
        """
        for attr_name in dir(self):
            if attr_name.startswith("__"):
                continue
            method = getattr(self, attr_name)
            if not callable(method):
                continue
            tool_name = getattr(method, "_mcp_tool_name", None)
            if tool_name is None:
                continue
            self._tool_registry[tool_name] = {
                "handler": method,
                "definition": Tool(
                    name=tool_name,
                    description=getattr(method, "_mcp_tool_description", ""),
                    parameters=getattr(method, "_mcp_tool_parameters", {}),
                ),
            }
        log.debug(
            "mcp.tools_registered",
            count=len(self._tool_registry),
            names=list(self._tool_registry),
        )

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[Tool]:
        """返回当前可用的工具列表（供 LLM function calling）。

        返回的 ``Tool`` 列表可直接传给 ``LLMProvider.chat(tools=...)``。
        """
        return [entry["definition"] for entry in self._tool_registry.values()]

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用指定工具并返回结果（JSON 字符串）。

        所有运行时异常（数据库错误、参数错误等）被捕获并转为
        ``{"error": "..."}`` JSON 字符串返回，确保 Agent Loop 总是
        拿到字符串结果而非异常。未知工具名同样返回 error JSON。

        Args:
            tool_name: 工具名称。
            arguments: 工具入参字典（由 LLM 的 tool_use.input 提供）。

        Returns:
            工具执行结果或错误信息（JSON 序列化字符串）。
        """
        entry = self._tool_registry.get(tool_name)
        if entry is None:
            log.warning(
                "mcp.unknown_tool",
                tool=tool_name,
                available=list(self._tool_registry),
            )
            return json.dumps(
                {
                    "error": f"未知工具: {tool_name}",
                    "available_tools": list(self._tool_registry),
                },
                ensure_ascii=False,
            )

        handler: Callable[..., Any] = entry["handler"]
        log.info("mcp.tool_call", tool=tool_name, arguments=arguments)
        try:
            return await handler(**arguments)
        except Exception as exc:
            log.error("mcp.tool_error", tool=tool_name, error=str(exc))
            return json.dumps(
                {"error": str(exc), "tool": tool_name},
                ensure_ascii=False,
            )

    # ------------------------------------------------------------------
    # 内部工具方法 — 每个方法独立，新增工具只需添加新方法
    # ------------------------------------------------------------------

    @mcp_tool(
        name="knowledge_search",
        description=(
            "搜索企业知识库，返回匹配的文档列表。"
            "支持按关键词进行全文检索，可选限定搜索的知识库范围。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "kb_id": {
                    "type": "string",
                    "description": "可选，限定搜索的知识库 ID（UUID 格式）",
                },
            },
            "required": ["query"],
        },
    )
    async def _tool_knowledge_search(
        self,
        query: str,
        kb_id: str | None = None,
    ) -> str:
        """搜索知识库 — 在 title 和 content_text 字段上执行 ILIKE 模糊匹配。"""
        pattern = f"%{query}%"
        stmt = (
            select(Document)
            .where(
                Document.deleted_at.is_(None),
                or_(
                    Document.title.ilike(pattern),
                    Document.content_text.ilike(pattern),
                ),
            )
            .order_by(Document.created_at.desc())
        )
        if kb_id is not None:
            stmt = stmt.where(Document.kb_id == uuid.UUID(kb_id))

        async with self._db_factory() as session:
            try:
                result = await session.execute(stmt)
                docs = list(result.scalars().all())
                results = [
                    {
                        "id": str(doc.id),
                        "title": doc.title,
                        "content_preview": (doc.content_text or "")[:200],
                        "kb_id": str(doc.kb_id),
                        "status": doc.status,
                        "classification": doc.classification,
                    }
                    for doc in docs
                ]
                await session.commit()
                return json.dumps(
                    {"results": results, "count": len(results)},
                    ensure_ascii=False,
                )
            except Exception:
                await session.rollback()
                raise

    @mcp_tool(
        name="document_get",
        description="获取文档详情，包括标题、内容、状态、密级等信息。",
        parameters={
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "文档 ID（UUID 格式）",
                },
            },
            "required": ["doc_id"],
        },
    )
    async def _tool_document_get(self, doc_id: str) -> str:
        """获取文档详情 — 通过 DocumentRepository 查询单条记录。"""
        async with self._db_factory() as session:
            try:
                repo = DocumentRepository(session)
                doc = await repo.get_by_id(uuid.UUID(doc_id))
                if doc is None:
                    await session.commit()
                    return json.dumps(
                        {"error": f"文档不存在: {doc_id}"},
                        ensure_ascii=False,
                    )
                result = {
                    "id": str(doc.id),
                    "title": doc.title,
                    "content": doc.content_text or "",
                    "content_html": doc.content_html or "",
                    "doc_type": doc.doc_type,
                    "status": doc.status,
                    "kb_id": str(doc.kb_id),
                    "classification": doc.classification,
                    "view_count": doc.view_count,
                    "created_at": doc.created_at.isoformat() if doc.created_at else None,
                }
                await session.commit()
                return json.dumps(result, ensure_ascii=False, default=str)
            except Exception:
                await session.rollback()
                raise

    @mcp_tool(
        name="document_create",
        description="在指定知识库中创建新文档，文档初始状态为 draft。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "文档标题"},
                "content": {"type": "string", "description": "文档纯文本内容"},
                "kb_id": {
                    "type": "string",
                    "description": "目标知识库 ID（UUID 格式）",
                },
            },
            "required": ["title", "content", "kb_id"],
        },
    )
    async def _tool_document_create(
        self,
        title: str,
        content: str,
        kb_id: str,
    ) -> str:
        """创建文档 — 以知识库所有者作为文档所有者，状态默认为 draft。"""
        async with self._db_factory() as session:
            try:
                kb_repo = KnowledgeBaseRepository(session)
                kb = await kb_repo.get_by_id(uuid.UUID(kb_id))
                if kb is None:
                    await session.commit()
                    return json.dumps(
                        {"error": f"知识库不存在: {kb_id}"},
                        ensure_ascii=False,
                    )

                doc_repo = DocumentRepository(session)
                doc = await doc_repo.create(
                    kb_id=uuid.UUID(kb_id),
                    title=title,
                    content_text=content,
                    owner_id=kb.owner_id,
                    status="draft",
                    doc_type="md",
                )
                result = {
                    "id": str(doc.id),
                    "title": doc.title,
                    "kb_id": str(doc.kb_id),
                    "status": doc.status,
                    "owner_id": str(doc.owner_id),
                }
                await session.commit()
                return json.dumps(result, ensure_ascii=False)
            except Exception:
                await session.rollback()
                raise

    @mcp_tool(
        name="query_oa_approval",
        description=(
            "查询 OA 系统审批状态。"
            "当前为 mock 实现，返回模拟审批流数据；"
            "接入真实 OA 系统后替换此方法体即可。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bill_no": {
                    "type": "string",
                    "description": "单据编号（如报销单号 BG2024001）",
                },
            },
            "required": ["bill_no"],
        },
    )
    async def _tool_query_oa_approval(self, bill_no: str) -> str:
        """查询 OA 审批状态 — Mock 实现。

        实际生产中应调用企业 OA 系统 API，此处返回模拟数据供开发联调。
        """
        # Mock 数据 — 实际应调用 OA 系统 API
        result = {
            "bill_no": bill_no,
            "status": "processing",
            "current_node": "部门经理审批",
            "submitter": "mock_user",
            "submit_time": "2026-07-06T10:00:00+00:00",
            "history": [
                {
                    "node": "发起申请",
                    "operator": "mock_user",
                    "time": "2026-07-06T09:00:00+00:00",
                    "action": "提交",
                },
                {
                    "node": "部门经理审批",
                    "operator": "mock_manager",
                    "time": "2026-07-06T10:00:00+00:00",
                    "action": "审批中",
                },
            ],
        }
        return json.dumps(result, ensure_ascii=False)

    @mcp_tool(
        name="create_it_ticket",
        description=(
            "创建 IT 服务台工单。"
            "当前为 mock 实现，返回模拟工单号；"
            "接入真实 IT 服务台后替换此方法体即可。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "工单标题"},
                "description": {"type": "string", "description": "问题描述"},
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                    "description": "优先级，默认 normal",
                },
            },
            "required": ["title", "description"],
        },
    )
    async def _tool_create_it_ticket(
        self,
        title: str,
        description: str,
        priority: str = "normal",
    ) -> str:
        """创建 IT 工单 — Mock 实现。

        实际生产中应调用 IT 服务台系统 API，此处返回模拟工单号供开发联调。
        """
        # Mock 数据 — 实际应调用 IT 服务台 API
        ticket_id = f"IT-{uuid.uuid4().hex[:8].upper()}"
        result = {
            "ticket_id": ticket_id,
            "title": title,
            "description": description,
            "priority": priority,
            "status": "open",
            "created_at": "2026-07-06T10:00:00+00:00",
        }
        return json.dumps(result, ensure_ascii=False)
