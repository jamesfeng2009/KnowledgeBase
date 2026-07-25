"""MCP 工具调用守卫测试 — 验证 DangerousToolGuard 和 engine 集成。

覆盖：
- DangerousToolGuard：check / confirm / is_confirmed / revoke / reset / block_result
- GuardResult / GuardAction：属性和序列化
- 默认配置：安全工具放行、危险工具拦截、未知工具放行
- 自定义配置：自定义危险/安全清单
- engine 集成：_execute_tool_use 守卫拦截 + 确认后放行
- P1-4: _execute_tool_use 改为 async generator，yield approval_required 事件
"""
from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.rag.tool_guard import (
    DangerousToolGuard,
    GuardAction,
    GuardResult,
    _DEFAULT_DANGEROUS_TOOLS,
    _SAFE_TOOLS,
)
from app.utils.sse import SSEEvent, SSEEventType


async def _drain_tool_use(
    engine: Any,
    state: dict[str, Any],
    tool_use: dict[str, Any],
    db: Any = None,
    user_uuid: Any = None,
) -> list[SSEEvent]:
    """排空 _execute_tool_use async generator，返回 yield 的 SSE 事件列表。

    P1-4: _execute_tool_use 从 async def 改为 AsyncIterator[SSEEvent]，
    需要用 async for 消费。支持传入 db / user_uuid 以测试审批流程。
    """
    events: list[SSEEvent] = []
    async for event in engine._execute_tool_use(
        state, tool_use, db=db, user_uuid=user_uuid
    ):
        events.append(event)
    return events


# ======================================================================
# GuardResult / GuardAction 测试
# ======================================================================


class TestGuardResult:
    """GuardResult 数据类测试。"""

    def test_allow_result(self) -> None:
        """ALLOW 动作的属性正确。"""
        result = GuardResult(action=GuardAction.ALLOW, tool_name="search", reason="只读")
        assert result.allowed is True
        assert result.blocked is False
        assert result.needs_confirmation is False

    def test_block_result(self) -> None:
        """BLOCK 动作的属性正确。"""
        result = GuardResult(action=GuardAction.BLOCK, tool_name="delete", reason="危险")
        assert result.allowed is False
        assert result.blocked is True
        assert result.needs_confirmation is False

    def test_confirm_result(self) -> None:
        """CONFIRM 动作的属性正确。"""
        result = GuardResult(action=GuardAction.CONFIRM, tool_name="create", reason="写操作")
        assert result.allowed is False
        assert result.blocked is False
        assert result.needs_confirmation is True

    def test_to_dict(self) -> None:
        """to_dict 返回正确字典。"""
        result = GuardResult(
            action=GuardAction.CONFIRM,
            tool_name="document_create",
            reason="创建文档",
            irreversible=False,
        )
        d = result.to_dict()
        assert d["action"] == "confirm"
        assert d["tool_name"] == "document_create"
        assert d["reason"] == "创建文档"
        assert d["irreversible"] is False


class TestGuardAction:
    """GuardAction 枚举测试。"""

    def test_enum_values(self) -> None:
        """枚举值正确。"""
        assert GuardAction.ALLOW.value == "allow"
        assert GuardAction.BLOCK.value == "block"
        assert GuardAction.CONFIRM.value == "confirm"


# ======================================================================
# DangerousToolGuard 核心测试
# ======================================================================


class TestDangerousToolGuardCheck:
    """DangerousToolGuard.check() 拦截逻辑测试。"""

    def test_safe_tool_allowed(self) -> None:
        """只读工具直接放行。"""
        guard = DangerousToolGuard()
        result = guard.check("knowledge_search", {"query": "test"})
        assert result.allowed is True
        assert result.action == GuardAction.ALLOW

    def test_dangerous_tool_needs_confirmation(self) -> None:
        """危险工具需要确认。"""
        guard = DangerousToolGuard()
        result = guard.check("document_create", {"title": "test"})
        assert result.needs_confirmation is True
        assert result.action == GuardAction.CONFIRM
        assert result.tool_name == "document_create"

    def test_irreversible_tool_marked(self) -> None:
        """不可逆工具标记 irreversible。"""
        guard = DangerousToolGuard()
        result = guard.check("create_it_ticket", {"title": "工单"})
        assert result.needs_confirmation is True
        assert result.irreversible is True

    def test_unknown_tool_allowed_with_warning(self) -> None:
        """未知工具默认放行。"""
        guard = DangerousToolGuard()
        result = guard.check("some_new_tool", {})
        assert result.allowed is True
        assert "默认放行" in result.reason

    def test_confirmed_tool_allowed(self) -> None:
        """已确认的危险工具放行。"""
        guard = DangerousToolGuard()
        # 先确认
        guard.confirm("document_create")
        # 再检查 — 应放行
        result = guard.check("document_create", {"title": "test"})
        assert result.allowed is True
        assert "用户已确认" in result.reason

    def test_check_with_none_input(self) -> None:
        """tool_input 为 None 时不报错。"""
        guard = DangerousToolGuard()
        result = guard.check("knowledge_search", None)
        assert result.allowed is True


class TestDangerousToolGuardConfirm:
    """DangerousToolGuard 确认管理测试。"""

    def test_confirm_and_is_confirmed(self) -> None:
        """确认后 is_confirmed 返回 True。"""
        guard = DangerousToolGuard()
        assert guard.is_confirmed("document_create") is False
        guard.confirm("document_create")
        assert guard.is_confirmed("document_create") is True

    def test_revoke_removes_confirmation(self) -> None:
        """revoke 撤销确认。"""
        guard = DangerousToolGuard()
        guard.confirm("document_create")
        assert guard.is_confirmed("document_create") is True
        guard.revoke("document_create")
        assert guard.is_confirmed("document_create") is False

    def test_reset_clears_all_confirmations(self) -> None:
        """reset 清空所有确认。"""
        guard = DangerousToolGuard()
        guard.confirm("document_create")
        guard.confirm("create_it_ticket")
        guard.reset()
        assert guard.is_confirmed("document_create") is False
        assert guard.is_confirmed("create_it_ticket") is False

    def test_revoke_unconfirmed_tool_no_error(self) -> None:
        """revoke 未确认的工具不报错。"""
        guard = DangerousToolGuard()
        guard.revoke("nonexistent")  # 不应抛异常


class TestDangerousToolGuardConfig:
    """DangerousToolGuard 自定义配置测试。"""

    def test_custom_dangerous_tools(self) -> None:
        """自定义危险工具清单。"""
        custom = {"custom_write_tool": {"reason": "自定义写操作", "irreversible": True}}
        guard = DangerousToolGuard(dangerous_tools=custom)

        result = guard.check("custom_write_tool", {})
        assert result.needs_confirmation is True
        assert result.irreversible is True

    def test_custom_safe_tools(self) -> None:
        """自定义安全工具清单。"""
        custom_safe = {"my_read_tool"}
        guard = DangerousToolGuard(safe_tools=custom_safe)

        result = guard.check("my_read_tool", {})
        assert result.allowed is True

    def test_empty_dangerous_tools_allows_everything(self) -> None:
        """空危险清单 — 所有工具放行（除显式安全工具外也是放行）。"""
        guard = DangerousToolGuard(dangerous_tools={})
        result = guard.check("document_create", {})
        # 不在危险清单中 → 未知工具 → 放行
        assert result.allowed is True

    def test_get_dangerous_tools(self) -> None:
        """get_dangerous_tools 返回配置副本。"""
        guard = DangerousToolGuard()
        tools = guard.get_dangerous_tools()
        assert "document_create" in tools
        assert "create_it_ticket" in tools
        # 修改返回值不影响内部状态
        tools["injected"] = {}
        assert "injected" not in guard.get_dangerous_tools()

    def test_block_result(self) -> None:
        """block_result 生成阻断结果。"""
        guard = DangerousToolGuard()
        result = guard.block_result("document_create", "用户未确认")
        assert result.blocked is True
        assert result.tool_name == "document_create"
        assert result.reason == "用户未确认"

    def test_default_configs_not_empty(self) -> None:
        """默认配置不为空。"""
        assert len(_DEFAULT_DANGEROUS_TOOLS) >= 2
        assert len(_SAFE_TOOLS) >= 3


# ======================================================================
# engine 集成测试 — _execute_tool_use 守卫拦截
# ======================================================================


class TestEngineToolGuardIntegration:
    """AgenticRAGEngine._execute_tool_use 守卫拦截集成测试。"""

    def _make_engine(self, guard: DangerousToolGuard | None = None) -> Any:
        """创建带 Mock 依赖的 AgenticRAGEngine。"""
        from app.rag.engine import AgenticRAGEngine

        mock_llm = MagicMock()
        mock_mcp = MagicMock()
        mock_retriever = MagicMock()
        mock_reranker = MagicMock()
        mock_generator = MagicMock()

        return AgenticRAGEngine(
            llm=mock_llm,
            mcp_client=mock_mcp,
            retriever=mock_retriever,
            reranker=mock_reranker,
            generator=mock_generator,
            tool_guard=guard,
        )

    @pytest.mark.asyncio
    async def test_safe_tool_executes_normally(self) -> None:
        """只读工具正常执行，不被守卫拦截。"""
        engine = self._make_engine()
        mock_mcp = engine.mcp
        mock_mcp.call_tool = AsyncMock(return_value='{"results": []}')

        state: dict[str, Any] = {"tool_results": []}
        tool_use = {"name": "knowledge_search", "input": {"query": "test"}, "id": "tu-1"}

        events = await _drain_tool_use(engine, state, tool_use)

        # 引擎调用时透传请求级租户 ID（本场景未设置，为 None）
        mock_mcp.call_tool.assert_called_once_with(
            "knowledge_search", {"query": "test"}, tenant_id=None
        )
        assert len(state["tool_results"]) == 1
        assert state["tool_results"][0]["tool"] == "knowledge_search"
        # 安全工具不产生 approval 事件
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_dangerous_tool_blocked_without_confirmation(self) -> None:
        """危险工具未确认时被阻断，不调用 MCP。"""
        engine = self._make_engine()
        mock_mcp = engine.mcp
        mock_mcp.call_tool = AsyncMock(return_value='{"id": "doc-1"}')

        state: dict[str, Any] = {"tool_results": []}
        tool_use = {"name": "document_create", "input": {"title": "test"}, "id": "tu-2"}

        events = await _drain_tool_use(engine, state, tool_use)

        # MCP 不应被调用
        mock_mcp.call_tool.assert_not_called()
        # 但 tool_results 中应有阻断信息
        assert len(state["tool_results"]) == 1
        result = json.loads(state["tool_results"][0]["result"])
        assert "需要用户确认" in result["error"]
        assert result["tool"] == "document_create"

    @pytest.mark.asyncio
    async def test_dangerous_tool_executes_after_confirmation(self) -> None:
        """确认后危险工具正常执行。"""
        guard = DangerousToolGuard()
        guard.confirm("document_create")
        engine = self._make_engine(guard=guard)
        mock_mcp = engine.mcp
        mock_mcp.call_tool = AsyncMock(return_value='{"id": "doc-1"}')

        state: dict[str, Any] = {"tool_results": []}
        tool_use = {"name": "document_create", "input": {"title": "test"}, "id": "tu-3"}

        await _drain_tool_use(engine, state, tool_use)

        mock_mcp.call_tool.assert_called_once()
        assert len(state["tool_results"]) == 1
        assert state["tool_results"][0]["tool"] == "document_create"

    @pytest.mark.asyncio
    async def test_unknown_tool_executes_normally(self) -> None:
        """未知工具默认放行。"""
        engine = self._make_engine()
        mock_mcp = engine.mcp
        mock_mcp.call_tool = AsyncMock(return_value='{"ok": true}')

        state: dict[str, Any] = {"tool_results": []}
        tool_use = {"name": "custom_tool", "input": {}, "id": "tu-4"}

        await _drain_tool_use(engine, state, tool_use)

        mock_mcp.call_tool.assert_called_once_with("custom_tool", {}, tenant_id=None)

    @pytest.mark.asyncio
    async def test_blocked_result_contains_irreversible_flag(self) -> None:
        """不可逆工具的阻断结果包含 irreversible 标记。"""
        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock()

        state: dict[str, Any] = {"tool_results": []}
        tool_use = {"name": "create_it_ticket", "input": {"title": "工单"}, "id": "tu-5"}

        await _drain_tool_use(engine, state, tool_use)

        result = json.loads(state["tool_results"][0]["result"])
        assert result["irreversible"] is True

    @pytest.mark.asyncio
    async def test_custom_guard_integration(self) -> None:
        """自定义守卫配置集成测试。"""
        custom_guard = DangerousToolGuard(
            dangerous_tools={"my_dangerous_tool": {"reason": "自定义危险", "irreversible": False}},
            safe_tools={"my_safe_tool"},
        )
        engine = self._make_engine(guard=custom_guard)
        engine.mcp.call_tool = AsyncMock(return_value='{"ok": true}')

        # 安全工具放行
        state1: dict[str, Any] = {"tool_results": []}
        await _drain_tool_use(
            engine, state1, {"name": "my_safe_tool", "input": {}, "id": "tu-6"}
        )
        assert len(state1["tool_results"]) == 1

        # 危险工具拦截
        state2: dict[str, Any] = {"tool_results": []}
        await _drain_tool_use(
            engine, state2, {"name": "my_dangerous_tool", "input": {}, "id": "tu-7"}
        )
        assert len(state2["tool_results"]) == 1
        result = json.loads(state2["tool_results"][0]["result"])
        assert "需要用户确认" in result["error"]

    @pytest.mark.asyncio
    async def test_default_guard_instantiated(self) -> None:
        """未注入守卫时使用默认实例。"""
        engine = self._make_engine()
        assert engine._tool_guard is not None
        assert isinstance(engine._tool_guard, DangerousToolGuard)

    @pytest.mark.asyncio
    async def test_guard_reset_between_sessions(self) -> None:
        """reset 后确认失效。"""
        guard = DangerousToolGuard()
        guard.confirm("document_create")
        assert guard.is_confirmed("document_create") is True

        guard.reset()
        assert guard.is_confirmed("document_create") is False

        # reset 后危险工具又被拦截
        result = guard.check("document_create", {})
        assert result.needs_confirmation is True

    @pytest.mark.asyncio
    async def test_blocked_tool_does_not_call_mcp(self) -> None:
        """被阻断的工具不会调用 MCP call_tool。"""
        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock()

        state: dict[str, Any] = {"tool_results": []}
        await _drain_tool_use(
            engine, state, {"name": "create_it_ticket", "input": {}, "id": "tu-8"}
        )

        engine.mcp.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_approval_required_event_without_db(self) -> None:
        """P1-4: db 为 None 时不创建审批记录，不 yield approval_required 事件。"""
        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock()

        state: dict[str, Any] = {
            "tool_results": [],
            "session_id": "test-session",
        }
        tool_use = {"name": "document_create", "input": {"title": "test"}, "id": "tu-9"}

        events = await _drain_tool_use(engine, state, tool_use)

        # 不传 db 时不应 yield approval_required 事件
        assert len(events) == 0
        # 但仍应有阻断信息
        assert len(state["tool_results"]) == 1
        engine.mcp.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_approval_required_event_with_db(self) -> None:
        """P1-4: 提供 db + user_uuid 时 yield approval_required SSE 事件。"""
        import uuid as uuid_module

        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock()

        # Mock ApprovalService.create_approval
        mock_approval = MagicMock()
        mock_approval.id = uuid_module.uuid4()

        mock_db = MagicMock()

        with patch(
            "app.services.approval_service.ApprovalService"
        ) as MockApprovalService:
            mock_service = MockApprovalService.return_value
            mock_service.create_approval = AsyncMock(return_value=mock_approval)

            state: dict[str, Any] = {
                "tool_results": [],
                "session_id": "test-session-2",
                "query": "测试问题",
                "user_id": "user-123",
                "iteration": 1,
                "max_iterations": 5,
                "messages": [],
                "retrieved_docs": [],
            }
            tool_use = {
                "name": "document_create",
                "input": {"title": "test"},
                "id": "tu-10",
            }

            events = await _drain_tool_use(
                engine, state, tool_use, db=mock_db, user_uuid=uuid_module.uuid4()
            )

            # 应 yield 1 个 approval_required 事件
            assert len(events) == 1
            assert events[0].event == SSEEventType.APPROVAL_REQUIRED
            event_data = events[0].data
            assert event_data["tool_name"] == "document_create"
            assert event_data["tool_use_id"] == "tu-10"
            assert "approval_id" in event_data
            assert "reason" in event_data

            # ApprovalService.create_approval 应被调用
            mock_service.create_approval.assert_called_once()
            call_kwargs = mock_service.create_approval.call_args
            assert call_kwargs.kwargs["tool_name"] == "document_create"
            assert call_kwargs.kwargs["tool_use_id"] == "tu-10"

            # MCP 不应被调用
            engine.mcp.call_tool.assert_not_called()
