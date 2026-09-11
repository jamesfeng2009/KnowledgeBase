"""MCP 工具路径用户级权限测试 — P1 核查。

覆盖（app/mcp/server.py 请求级用户上下文 _user_ctx）：
- _resolve_document_permission 三态：无用户上下文（租户范围契约）/
  用户加载成功（ABAC 过滤）/ 用户加载失败（fail-closed）；
- knowledge_search：ABAC 过滤（可见性 + 密级）、fail-closed 拒绝；
- document_get：越权按"不存在"返回（不泄漏存在性）、fail-closed；
- document_create：check_write 写权限校验（owner 放行 / 非成员拒绝）、
  无用户上下文维持租户范围契约（外部服务身份既有行为）；
- call_tool 调用结束后用户上下文自动复位（无跨请求泄漏）。

mock 策略：FakeSession 按调用顺序吐出预设查询结果，
不依赖真实数据库（与 test_retrieval_invariants.py 同款思路）。
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import app.mcp.server as mcp_server_module
from app.mcp.server import KnowledgeBaseMCPServer, _PERM_FAIL_CLOSED


# ======================================================================
# Fake 基础设施
# ======================================================================


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _Result:
    """模拟 SQLAlchemy Result：同时支持 scalars() 与原生 all()。"""

    def __init__(self, rows: list[Any]) -> None:
        self._scalars = _Scalars(rows)

    def scalars(self) -> _Scalars:
        return self._scalars

    def all(self) -> list[Any]:
        return list(self._scalars._rows)


class FakeSession:
    """按调用顺序吐出预设结果集的假 AsyncSession。

    execute 每被调用一次，从 results 队列弹出下一组行；队列耗尽时
    返回空结果（fail-closed 语义：查不到 → 拒绝）。记录 add / commit /
    rollback 以便断言写路径是否被触达。
    """

    def __init__(self, results: list[list[Any]] | None = None) -> None:
        self._results: list[list[Any]] = list(results or [])
        self.execute_count = 0
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    # 工具经 ``async with self._db_factory() as session`` 持有会话
    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self, stmt: Any) -> _Result:
        self.execute_count += 1
        rows = self._results.pop(0) if self._results else []
        return _Result(rows)

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        for inst in self.added:
            if getattr(inst, "id", None) is None:
                inst.id = uuid4()

    async def refresh(self, instance: Any) -> None:  # pragma: no cover - no-op
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def get(self, model: Any, ident: Any) -> Any:  # pragma: no cover
        return None


def _make_server(session: FakeSession) -> KnowledgeBaseMCPServer:
    return KnowledgeBaseMCPServer(db_factory=lambda: session)


def _make_user(
    role: str = "editor",
    clearance: str = "internal",
    owner_of: uuid.UUID | None = None,
) -> SimpleNamespace:
    """构造 PermissionService 兼容的用户桩（含 P1 visibility 所需字段）。"""
    return SimpleNamespace(
        id=owner_of or uuid4(),
        role=role,
        clearance_level=clearance,
        dept_id=None,
    )


def _make_doc(
    kb_id: uuid.UUID,
    classification: str = "internal",
    owner_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    """构造 Document 桩（覆盖 knowledge_search / document_get 所需字段）。"""
    return SimpleNamespace(
        id=uuid4(),
        title=f"title-{uuid4().hex[:6]}",
        content_text="正文内容",
        content_html="<p>正文内容</p>",
        doc_type="md",
        status="published",
        kb_id=kb_id,
        classification=classification,
        view_count=0,
        created_at=None,
        owner_id=owner_id,
    )


def _parse(raw: str) -> dict[str, Any]:
    return json.loads(raw)


# ======================================================================
# _resolve_document_permission 三态
# ======================================================================


class TestResolveDocumentPermission:
    @pytest.mark.asyncio
    async def test_no_user_ctx_returns_none(self) -> None:
        """无用户上下文（外部 API Key 服务身份）→ None，维持租户范围契约。"""
        session = FakeSession()
        server = _make_server(session)
        # call_tool 不传 user_id → _user_ctx 未设置
        await server.call_tool("read_tool_result", {"path": "x/y.txt"})
        assert mcp_server_module._current_user_id() is None
        # read_tool_result 不查库 → 无 execute 发生在权限解析上
        assert session.execute_count == 0

    @pytest.mark.asyncio
    async def test_user_found_returns_permission_service(self) -> None:
        """用户加载成功 → 返回 PermissionService（携带请求级用户上下文）。"""
        user_id = uuid4()
        session = FakeSession(results=[[_make_user()]])
        token = mcp_server_module._user_ctx.set(user_id)
        try:
            perm = await mcp_server_module._resolve_document_permission(session)
        finally:
            mcp_server_module._user_ctx.reset(token)
        assert not isinstance(perm, str)
        assert perm is not None and hasattr(perm, "filter_documents")

    @pytest.mark.asyncio
    async def test_user_not_found_fail_closed(self) -> None:
        """user_id 已设置但用户查不到 → fail-closed 哨兵。"""
        session = FakeSession(results=[[]])
        token = mcp_server_module._user_ctx.set(uuid4())
        try:
            perm = await mcp_server_module._resolve_document_permission(session)
        finally:
            mcp_server_module._user_ctx.reset(token)
        assert perm == _PERM_FAIL_CLOSED


# ======================================================================
# knowledge_search — 用户级 ABAC 过滤
# ======================================================================


class TestKnowledgeSearchPermission:
    @pytest.mark.asyncio
    async def test_no_user_ctx_returns_tenant_scope(self) -> None:
        """无用户上下文 — 不做用户级过滤（租户范围既有契约）。"""
        kb = uuid4()
        docs = [_make_doc(kb, "internal"), _make_doc(kb, "secret")]
        session = FakeSession(results=[docs])
        server = _make_server(session)
        out = _parse(await server.call_tool("knowledge_search", {"query": "报销"}))
        assert out["count"] == 2  # 越密级文档同样返回（仅租户范围）
        assert session.execute_count == 1  # 仅检索查询，无权限解析查询

    @pytest.mark.asyncio
    async def test_user_ctx_filters_by_visibility_and_classification(self) -> None:
        """用户上下文 — filter_documents 双重过滤（kb 可见性 + 密级）。"""
        kb = uuid4()
        allowed = _make_doc(kb, "internal")
        over_secret = _make_doc(kb, "secret")  # 同库但越密级
        user = _make_user(role="editor", clearance="internal")
        session = FakeSession(
            results=[
                [allowed, over_secret],  # #1 检索结果
                [user],                  # #2 用户加载
                [(kb,)],                 # #3 可访问 kb 集合（含 kb）
            ]
        )
        server = _make_server(session)
        out = _parse(
            await server.call_tool(
                "knowledge_search", {"query": "报销"}, user_id=str(user.id)
            )
        )
        ids = {r["id"] for r in out["results"]}
        assert ids == {str(allowed.id)}  # 越密级文档被剔除
        assert out["count"] == 1

    @pytest.mark.asyncio
    async def test_user_ctx_kb_visibility_excludes_docs(self) -> None:
        """文档所属 kb 不在用户可访问集合 → 剔除（public/dept 之外）。"""
        kb = uuid4()
        other_kb_doc = _make_doc(uuid4(), "internal")  # 别的私有库
        user = _make_user()
        session = FakeSession(
            results=[
                [other_kb_doc],
                [user],
                [(kb,)],  # 可访问集合不含 other_kb
            ]
        )
        server = _make_server(session)
        out = _parse(
            await server.call_tool(
                "knowledge_search", {"query": "x"}, user_id=str(user.id)
            )
        )
        assert out["results"] == []
        assert out["count"] == 0

    @pytest.mark.asyncio
    async def test_user_load_failure_fail_closed(self) -> None:
        """用户解析失败 — 拒绝而非回落租户范围（宁可拒绝不可越权）。"""
        docs = [_make_doc(uuid4())]
        session = FakeSession(results=[docs, []])  # #2 用户查不到
        server = _make_server(session)
        out = _parse(
            await server.call_tool(
                "knowledge_search", {"query": "x"}, user_id=str(uuid4())
            )
        )
        assert out["results"] == []
        assert "无权访问" in out["error"]

    @pytest.mark.asyncio
    async def test_user_ctx_reset_after_call(self) -> None:
        """call_tool 结束后用户上下文复位 — 并发请求互不串扰。"""
        user = _make_user()
        session = FakeSession(
            results=[[_make_doc(uuid4())], [user], []]
        )
        server = _make_server(session)
        await server.call_tool(
            "knowledge_search", {"query": "x"}, user_id=str(user.id)
        )
        assert mcp_server_module._current_user_id() is None


# ======================================================================
# document_get — 越权按"不存在"返回 + fail-closed
# ======================================================================


class TestDocumentGetPermission:
    @pytest.mark.asyncio
    async def test_allowed_user_reads_document(self) -> None:
        """可访问范围内的文档正常返回全文。"""
        kb = uuid4()
        user = _make_user(owner_of=None)
        doc = _make_doc(kb, "internal")
        session = FakeSession(results=[[doc], [user], [(kb,)]])
        server = _make_server(session)
        out = _parse(
            await server.call_tool("document_get", {"doc_id": str(doc.id)},
                                   user_id=str(user.id))
        )
        assert out.get("id") == str(doc.id)
        assert out.get("content") == "正文内容"

    @pytest.mark.asyncio
    async def test_denied_user_mimics_not_found(self) -> None:
        """越权文档按"不存在"返回 — 不泄漏文档存在性。"""
        doc = _make_doc(uuid4(), "internal")
        user = _make_user()
        session = FakeSession(results=[[doc], [user], []])  # 无可访问 kb
        server = _make_server(session)
        out = _parse(
            await server.call_tool("document_get", {"doc_id": str(doc.id)},
                                   user_id=str(user.id))
        )
        assert out == {"error": f"文档不存在: {doc.id}"}

    @pytest.mark.asyncio
    async def test_user_load_failure_fail_closed(self) -> None:
        """用户解析失败 — 按不存在拒绝，不返回内容。"""
        doc = _make_doc(uuid4())
        session = FakeSession(results=[[doc], []])
        server = _make_server(session)
        out = _parse(
            await server.call_tool("document_get", {"doc_id": str(doc.id)},
                                   user_id=str(uuid4()))
        )
        assert out == {"error": f"文档不存在: {doc.id}"}


# ======================================================================
# document_create — check_write 写权限校验
# ======================================================================


class TestDocumentCreatePermission:
    @pytest.mark.asyncio
    async def test_owner_can_create(self) -> None:
        """kb owner 经工具路径创建草稿 — check_write 放行。"""
        owner = _make_user(role="editor")
        kb = SimpleNamespace(id=uuid4(), owner_id=owner.id, visibility="private")
        session = FakeSession(results=[[kb], [owner], [kb]])  # #3 check_write 复查 kb
        server = _make_server(session)
        out = _parse(
            await server.call_tool(
                "document_create",
                {"title": "新文档", "content": "内容", "kb_id": str(kb.id)},
                user_id=str(owner.id),
            )
        )
        assert "error" not in out
        assert out["status"] == "draft"
        assert out["kb_id"] == str(kb.id)
        assert len(session.added) == 1  # 写路径真实触达

    @pytest.mark.asyncio
    async def test_non_member_denied(self) -> None:
        """非 owner 且非成员 — check_write 拒绝，与"不存在"同文案。"""
        user = _make_user(role="editor")
        kb = SimpleNamespace(id=uuid4(), owner_id=uuid4(), visibility="dept")
        session = FakeSession(
            results=[
                [kb],    # #1 kb 查询
                [user],  # #2 用户加载
                [kb],    # #3 check_write 复查 kb（非 owner）
                [],      # #4 成员查询（admin|editor 成员）→ 空
            ]
        )
        server = _make_server(session)
        out = _parse(
            await server.call_tool(
                "document_create",
                {"title": "新文档", "content": "内容", "kb_id": str(kb.id)},
                user_id=str(user.id),
            )
        )
        assert out == {"error": f"知识库不存在: {kb.id}"}
        assert session.added == []  # 写路径未被触达
        assert session.rollbacks >= 1

    @pytest.mark.asyncio
    async def test_no_user_ctx_keeps_tenant_scope(self) -> None:
        """无用户上下文（外部服务身份）— 维持租户范围既有契约可写。"""
        kb = SimpleNamespace(id=uuid4(), owner_id=uuid4(), visibility="private")
        session = FakeSession(results=[[kb]])
        server = _make_server(session)
        out = _parse(
            await server.call_tool(
                "document_create",
                {"title": "新文档", "content": "内容", "kb_id": str(kb.id)},
            )
        )
        assert "error" not in out
        assert out["status"] == "draft"

    @pytest.mark.asyncio
    async def test_user_load_failure_fail_closed(self) -> None:
        """用户解析失败 — fail-closed 拒绝写入。"""
        kb = SimpleNamespace(id=uuid4(), owner_id=uuid4(), visibility="private")
        session = FakeSession(results=[[kb], []])
        server = _make_server(session)
        out = _parse(
            await server.call_tool(
                "document_create",
                {"title": "t", "content": "c", "kb_id": str(kb.id)},
                user_id=str(uuid4()),
            )
        )
        assert out == {"error": f"知识库不存在: {kb.id}"}
        assert session.added == []
