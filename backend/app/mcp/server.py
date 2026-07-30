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

import contextvars
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
from app.utils.tenant import apply_tenant_filter

log = get_logger(__name__)

# 请求级租户上下文 — Server 为全局单例（随 RAG 引擎复用），租户 ID 不能
# 存实例属性（并发请求互相覆盖），用 ContextVar 按请求任务隔离。
# 由 call_tool(tenant_id=...) 设置，工具方法内读取并追加租户过滤；
# 未设置时不过滤（兼容脚本/单租户场景）。
_tenant_ctx: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "mcp_request_tenant_id", default=None
)


def _current_tenant() -> uuid.UUID | None:
    """读取当前请求上下文的租户 ID（未设置返回 None）。"""
    return _tenant_ctx.get()


#: knowledge_search 单工具结果上限 — LLM 传入泛词（如"系统"）时
#: 无界结果集会撑爆上下文窗口，截断并标注 truncated 让 Agent 自行改写查询。
_SEARCH_RESULT_LIMIT: int = 20


def mcp_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    category: str = "general",
    tags: list[str] | None = None,
    skill_description: str = "",
    long_running: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器：将方法注册为 MCP 工具。

    开闭原则落点：新增工具只需用此装饰器标注一个新的异步方法，
    ``_scan_tools`` 会自动收集，``list_tools`` / ``call_tool`` 自动分发，
    无需修改 ``KnowledgeBaseMCPServer`` 的任何既有代码。

    渐进式技能加载（Find Skills）：``category`` / ``tags`` / ``skill_description``
    用于构建轻量技能索引，Agent Loop 先匹配相关技能再按需加载完整 schema，
    避免工具数量增长后全量加载浪费 token。

    长耗时任务（对齐 MCP 2026-07-28 Tasks 扩展）：``long_running=True`` 时，
    HTTP API 层会通过 ``call_tool_async`` 创建持久化 taskId 返回给客户端，
    客户端轮询 ``GET /mcp/tasks/{task_id}`` 获取最终结果，不阻塞 HTTP 连接。

    Args:
        name: 工具名称（对应 LLM function calling 的 function name）。
        description: 工具描述，供 LLM 决策调用。
        parameters: 工具入参 JSON Schema（``{"type": "object", "properties": ...}``）。
        category: 工具分类（如 ``search`` / ``document`` / ``workflow`` / ``analytics``），
            用于 Find Skills 分组匹配。
        tags: 工具标签列表（如 ``["全文检索", "知识库"]``），用于关键词匹配。
        skill_description: 技能详细描述（比 ``description`` 更长，仅在技能激活时加载，
            空时回退到 ``description``）。
        long_running: 标记为长耗时工具。``True`` 时 HTTP API 层自动转为异步任务模式
            （返回 taskId 句柄而非阻塞等待）。Agent Loop 内部调用仍走同步 ``call_tool``。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func._mcp_tool_name = name  # type: ignore[attr-defined]
        func._mcp_tool_description = description  # type: ignore[attr-defined]
        func._mcp_tool_parameters = parameters  # type: ignore[attr-defined]
        func._mcp_tool_category = category  # type: ignore[attr-defined]
        func._mcp_tool_tags = tags or []  # type: ignore[attr-defined]
        func._mcp_tool_skill_description = skill_description or description  # type: ignore[attr-defined]
        func._mcp_tool_long_running = long_running  # type: ignore[attr-defined]
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

        注册表结构::

            {
                "knowledge_search": {
                    "handler": method,
                    "definition": Tool(...),
                    "category": "search",
                    "tags": ["全文检索", "知识库"],
                    "skill_description": "...",
                },
            }
        """
        for attr_name in dir(self):
            if attr_name.startswith("__"):
                continue
            method = getattr(self, attr_name)
            if not callable(method):
                continue
            tool_name = getattr(method, "_mcp_tool_name", None)
            if tool_name is None or not isinstance(tool_name, str):
                continue
            self._tool_registry[tool_name] = {
                "handler": method,
                "definition": Tool(
                    name=tool_name,
                    description=getattr(method, "_mcp_tool_description", ""),
                    parameters=getattr(method, "_mcp_tool_parameters", {}),
                ),
                "category": getattr(method, "_mcp_tool_category", "general"),
                "tags": getattr(method, "_mcp_tool_tags", []),
                "skill_description": getattr(
                    method, "_mcp_tool_skill_description",
                    getattr(method, "_mcp_tool_description", ""),
                ),
                "long_running": getattr(method, "_mcp_tool_long_running", False),
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

    async def list_tools_by_names(self, names: list[str]) -> list[Tool]:
        """按名称子集返回工具列表 — Find Skills 按需加载入口。

        只有被 SkillFinder 匹配到的工具才会加载完整 schema，
        避免全量加载浪费 token。未找到的名称静默跳过。

        Args:
            names: 需要加载的工具名称列表。

        Returns:
            匹配到的 Tool 列表（可能为空）。
        """
        result: list[Tool] = []
        for name in names:
            entry = self._tool_registry.get(name)
            if entry is not None:
                result.append(entry["definition"])
        return result

    def get_skill_index(self) -> list[dict[str, Any]]:
        """返回轻量技能索引 — 仅 name / category / tags / description。

        用于 SkillFinder 意图匹配，token 开销极小（每个技能约 20-30 token），
        对比全量加载 schema（每个工具 200-500 token）。
        """
        return [
            {
                "name": name,
                "category": entry.get("category", "general"),
                "tags": entry.get("tags", []),
                "description": entry.get("skill_description", entry["definition"]["description"]),
            }
            for name, entry in self._tool_registry.items()
        ]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        *,
        tenant_id: str | uuid.UUID | None = None,
    ) -> str:
        """调用指定工具并返回结果（JSON 字符串）。

        所有运行时异常（数据库错误、参数错误等）被捕获并转为
        ``{"error": "..."}`` JSON 字符串返回，确保 Agent Loop 总是
        拿到字符串结果而非异常。未知工具名同样返回 error JSON。

        Args:
            tool_name: 工具名称。
            arguments: 工具入参字典（由 LLM 的 tool_use.input 提供）。
            tenant_id: 请求级租户 ID（由调用方从请求上下文传入，
                **不信任** LLM 在 arguments 中自封的租户标识）。
                设置期间工具内查询追加租户过滤，调用结束后自动复位。

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

        # 解析并设置请求级租户上下文（非法 ID 视为未设置，走不过滤兜底）
        tenant_uuid: uuid.UUID | None = None
        if tenant_id is not None:
            try:
                tenant_uuid = (
                    tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
                )
            except (ValueError, TypeError):
                log.warning("mcp.invalid_tenant_id", tenant_id=str(tenant_id))
        token = _tenant_ctx.set(tenant_uuid)
        try:
            return await handler(**arguments)
        except Exception as exc:
            log.error("mcp.tool_error", tool=tool_name, error=str(exc))
            return json.dumps(
                {"error": str(exc), "tool": tool_name},
                ensure_ascii=False,
            )
        finally:
            _tenant_ctx.reset(token)

    def is_long_running(self, tool_name: str) -> bool:
        """查询工具是否标记为长耗时（需走异步任务模式）。

        HTTP API 层据此决定是同步返回结果还是创建 taskId 句柄。
        Agent Loop 内部调用 ``call_tool`` 时不受此标记影响（始终同步）。

        Args:
            tool_name: 工具名称。

        Returns:
            ``True`` 表示该工具标记为 ``long_running``，未知工具返回 ``False``。
        """
        entry = self._tool_registry.get(tool_name)
        return entry is not None and entry.get("long_running", False)

    async def call_tool_async(
        self,
        tool_name: str,
        arguments: dict,
        *,
        tenant_id: str | uuid.UUID | None = None,
    ) -> str:
        """异步调用长耗时工具 — 返回任务句柄而非阻塞等待结果。

        对齐 MCP 2026-07-28 规范 Tasks 扩展的核心语义：
        - 创建持久化 taskId，客户端凭此 ID 轮询状态
        - 后台通过 ``asyncio.create_task`` 执行工具逻辑
        - 结果写入 TaskStore（Redis），客户端可断线重连后取回

        与 ``call_tool`` 的区别：
        - ``call_tool`` 阻塞等待并返回工具结果（Agent Loop 用）
        - ``call_tool_async`` 立即返回 taskId 句柄（HTTP API 层用）

        Args:
            tool_name: 工具名称。
            arguments: 工具入参字典。
            tenant_id: 请求级租户 ID。

        Returns:
            JSON 字符串，包含 ``task_id`` / ``status`` / ``poll_interval_ms`` / ``ttl_ms``。
            工具不存在时返回与 ``call_tool`` 相同的 error JSON。
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

        # 惰性导入 TaskStore — 避免 server.py 模块加载时强依赖 Redis
        from app.mcp.task_store import get_task_store

        store = get_task_store()
        tenant_str = str(tenant_id) if tenant_id is not None else None
        task_id = await store.create_task(
            tool_name=tool_name,
            arguments=arguments,
            tenant_id=tenant_str,
        )

        # 后台执行 — 闭包捕获所需上下文，不依赖请求生命周期
        async def _execute() -> None:
            try:
                result_str = await self.call_tool(
                    tool_name, arguments, tenant_id=tenant_id,
                )
                # 尝试解析为 dict 便于客户端消费；解析失败保留原始字符串
                try:
                    result_data: Any = json.loads(result_str)
                except (json.JSONDecodeError, TypeError):
                    result_data = result_str
                await store.complete_task(task_id, result_data)
            except Exception as exc:
                log.error(
                    "mcp.task_execution_error",
                    task_id=task_id, tool=tool_name, error=str(exc),
                )
                await store.fail_task(task_id, str(exc))

        # 创建后台任务 — 不 await，立即返回
        import asyncio

        asyncio.create_task(_execute())

        log.info(
            "mcp.task_created",
            task_id=task_id,
            tool=tool_name,
            poll_interval_ms=store.poll_interval_ms,
        )

        return json.dumps(
            {
                "task_id": task_id,
                "status": "working",
                "poll_interval_ms": store.poll_interval_ms,
                "ttl_ms": store.ttl_seconds * 1000,
            },
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
            "适用场景：用户想查找、搜索、了解某主题的相关文档。"
            "不适用于：已知具体文档 ID 的查询（应改用 document_get 获取完整详情）；"
            "查询 OA 审批状态（应改用 query_oa_approval）；"
            "创建工单或文档（应改用对应写操作工具）。"
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
        category="search",
        tags=["全文检索", "知识库", "搜索", "search", "文档", "查找", "了解"],
        skill_description=(
            "在企业知识库中按关键词进行全文检索，返回匹配的文档列表。"
            "支持限定特定知识库范围。"
            "负向边界：不要用于已知文档 ID 的精确查询（用 document_get），"
            "不要用于查审批状态（用 query_oa_approval）。"
        ),
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
            # 无界结果集会被 LLM 泛词撑爆（全表命中拼 JSON 进上下文）
            .limit(_SEARCH_RESULT_LIMIT)
        )
        if kb_id is not None:
            stmt = stmt.where(Document.kb_id == uuid.UUID(kb_id))
        # 租户隔离 — MCP Server 裸建会话无 RLS GUC 注入（fail-open），
        # 必须在应用层过滤，否则跨租户文档全文泄漏。
        stmt = apply_tenant_filter(stmt, Document, _current_tenant())

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
                    {
                        "results": results,
                        "count": len(results),
                        "truncated": len(results) >= _SEARCH_RESULT_LIMIT,
                    },
                    ensure_ascii=False,
                )
            except Exception:
                await session.rollback()
                raise

    @mcp_tool(
        name="document_get",
        description=(
            "获取文档详情，包括标题、内容、状态、密级等信息。"
            "适用场景：用户已知文档 ID，需要查看文档的完整内容或元信息。"
            "不适用于：按关键词搜索文档列表（应改用 knowledge_search）；"
            "创建新文档（应改用 document_create）。"
        ),
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
        category="document",
        tags=["文档", "详情", "查看", "document", "get", "内容"],
        skill_description=(
            "获取指定文档的详细信息，包括标题、内容、状态、密级等字段。需要提供文档 ID。"
            "负向边界：不要用于关键词搜索（用 knowledge_search），"
            "不要用于创建文档（用 document_create）。"
        ),
    )
    async def _tool_document_get(self, doc_id: str) -> str:
        """获取文档详情 — 通过 DocumentRepository 查询单条记录。"""
        async with self._db_factory() as session:
            try:
                # 租户隔离 — 仓储层自动过滤（跨租户文档按"不存在"返回，
                # 避免泄漏文档存在性）
                repo = DocumentRepository(session, tenant_id=_current_tenant())
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
        description=(
            "在指定知识库中创建新文档，文档初始状态为 draft。"
            "适用场景：用户想要创建、新建、编写文档。"
            "不适用于：搜索已有文档（应改用 knowledge_search）；"
            "查看已有文档内容（应改用 document_get）；"
            "修改/删除已有文档（当前不支持，需人工操作）。"
        ),
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
        category="document",
        tags=["文档", "创建", "新建", "create", "写入", "draft", "编写", "新增"],
        skill_description=(
            "在指定知识库中创建新文档，文档初始状态为 draft 草稿。"
            "需要提供标题、内容和目标知识库 ID。"
            "负向边界：不要用于搜索文档（用 knowledge_search），"
            "不要用于查看文档（用 document_get）。"
        ),
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
                # 租户隔离 — KB 校验与文档创建均限定在当前租户内：
                # 跨租户 KB 按"不存在"拒绝，新文档自动注入 tenant_id。
                tenant = _current_tenant()
                kb_repo = KnowledgeBaseRepository(session, tenant_id=tenant)
                kb = await kb_repo.get_by_id(uuid.UUID(kb_id))
                if kb is None:
                    await session.commit()
                    return json.dumps(
                        {"error": f"知识库不存在: {kb_id}"},
                        ensure_ascii=False,
                    )

                doc_repo = DocumentRepository(session, tenant_id=tenant)
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
            "适用场景：用户想查询单据审批进度、审批到哪个节点。"
            "不适用于：搜索知识库文档（应改用 knowledge_search）；"
            "创建 IT 工单（应改用 create_it_ticket）。"
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
        category="workflow",
        tags=["OA", "审批", "流程", "查询", "approval", "单据", "进度", "报销"],
        skill_description=(
            "查询 OA 系统的审批流程状态，包括当前审批节点、提交人、审批意见等信息。"
            "需要提供单据编号。"
            "负向边界：不要用于搜索文档（用 knowledge_search），"
            "不要用于创建工单（用 create_it_ticket）。"
        ),
        long_running=True,
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
            "适用场景：用户想报修、提工单、寻求 IT 支持。"
            "不适用于：查询已有工单状态（当前不支持查询）；"
            "查询 OA 审批进度（应改用 query_oa_approval）；"
            "搜索知识库文档（应改用 knowledge_search）。"
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
        category="workflow",
        tags=["IT", "工单", "创建", "ticket", "服务台", "报修", "提单", "支持"],
        skill_description=(
            "创建 IT 服务台工单，支持设置优先级（low/normal/high/urgent）。"
            "需要提供工单标题和问题描述。"
            "负向边界：不要用于查询审批进度（用 query_oa_approval），"
            "不要用于搜索文档（用 knowledge_search）。"
        ),
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

    @mcp_tool(
        name="batch_analyze_documents",
        description=(
            "批量分析知识库文档 — 对指定知识库中的文档执行摘要、标签提取和分类。"
            "当前为 mock 实现，模拟批量处理延迟；"
            "接入真实 LLM 批处理管线后替换此方法体即可。"
            "适用场景：用户想批量处理、分析、归档大量文档。"
            "不适用于：搜索单个文档（应改用 knowledge_search）；"
            "查看单个文档详情（应改用 document_get）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kb_id": {
                    "type": "string",
                    "description": "目标知识库 ID（UUID 格式）",
                },
                "limit": {
                    "type": "integer",
                    "description": "批量处理上限，默认 50",
                    "default": 50,
                },
            },
            "required": ["kb_id"],
        },
        category="analytics",
        tags=["批量", "分析", "摘要", "标签", "分类", "batch", "analyze", "归档"],
        skill_description=(
            "对指定知识库中的文档执行批量智能分析（摘要+标签+分类）。"
            "需要提供知识库 ID，可选限制处理数量。"
            "负向边界：不要用于搜索文档（用 knowledge_search），"
            "不要用于查看单个文档（用 document_get）。"
        ),
        long_running=True,
    )
    async def _tool_batch_analyze_documents(
        self,
        kb_id: str,
        limit: int = 50,
    ) -> str:
        """批量分析文档 — Mock 实现，模拟批量处理延迟。

        实际生产中应调用 LLM 批处理管线，此处用 asyncio.sleep 模拟延迟。
        """
        import asyncio

        # 模拟批量处理延迟 — 按 limit 比例延迟（每 10 个文档约 1 秒）
        delay = min(max(limit // 10, 1), 5)
        await asyncio.sleep(delay)

        result = {
            "kb_id": kb_id,
            "processed": limit,
            "summary_generated": limit,
            "tags_extracted": limit,
            "classified": limit,
            "duration_seconds": delay,
            "status": "completed",
        }
        return json.dumps(result, ensure_ascii=False)
