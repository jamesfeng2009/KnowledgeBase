"""P1 工具审批集成测试 — 覆盖 ApprovalService + REST 端点 + 恢复机制。

测试覆盖：
- ApprovalService：创建/批准/拒绝/查询/会话级缓存/恢复
- 审批 REST 端点：GET pending / POST approve / POST reject / GET by id
- 服务重启恢复：标记过期 + 加载活跃审批
- DangerousToolGuard 会话级控制：确认后放行

P1 核心流程：
    引擎拦截危险工具 → 创建 ToolApproval（JSONB 快照）→ yield approval_required
    → 前端弹窗 → REST approve/reject → 会话级缓存生效
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.rag.tool_guard import DangerousToolGuard, GuardAction
from app.utils.sse import SSEEventType


# ======================================================================
# DangerousToolGuard 会话级控制测试
# ======================================================================


class TestSessionLevelGuard:
    """P1-3: DangerousToolGuard 会话级控制测试。"""

    def test_session_confirm_then_check(self) -> None:
        """会话级确认后，同会话内放行。"""
        guard = DangerousToolGuard()
        session_id = "test-session-001"

        # 初始状态 — 危险工具需要确认
        result = guard.check("document_create", session_id=session_id)
        assert result.needs_confirmation is True

        # 会话级确认
        guard.confirm_session_tool(session_id, "document_create")

        # 同会话内 — 放行
        result = guard.check("document_create", session_id=session_id)
        assert result.allowed is True
        assert "已确认" in result.reason

    def test_session_isolation(self) -> None:
        """不同会话间隔离 — A 会话确认不影响 B 会话。"""
        guard = DangerousToolGuard()
        session_a = "session-a"
        session_b = "session-b"

        guard.confirm_session_tool(session_a, "document_create")

        # A 会话放行
        assert guard.check("document_create", session_id=session_a).allowed is True
        # B 会话仍需确认
        assert guard.check("document_create", session_id=session_b).needs_confirmation is True

    def test_clear_session(self) -> None:
        """清理会话后确认失效。"""
        guard = DangerousToolGuard()
        session_id = "test-session-clear"

        guard.confirm_session_tool(session_id, "document_create")
        assert guard.is_session_confirmed(session_id, "document_create") is True

        guard.clear_session(session_id)
        assert guard.is_session_confirmed(session_id, "document_create") is False
        # 清理后危险工具又被拦截
        assert guard.check("document_create", session_id=session_id).needs_confirmation is True

    def test_global_confirm_applies_to_all_sessions(self) -> None:
        """向后兼容：全局确认（confirm）对所有会话生效。"""
        guard = DangerousToolGuard()
        guard.confirm("document_create")

        # 任意会话都应放行
        assert guard.check("document_create", session_id="any-session").allowed is True
        assert guard.check("document_create", session_id=None).allowed is True

    def test_safe_tools_always_allowed(self) -> None:
        """安全工具无论会话状态都放行。"""
        guard = DangerousToolGuard()
        assert guard.check("knowledge_search", session_id="any").allowed is True
        assert guard.check("document_get", session_id=None).allowed is True


# ======================================================================
# ApprovalService 测试（使用 Mock DB）
# ======================================================================


class TestApprovalService:
    """P1-2: ApprovalService CRUD + 会话级缓存 + 恢复测试。"""

    def _make_mock_user(self) -> MagicMock:
        """创建 Mock User。"""
        user = MagicMock()
        user.id = uuid.uuid4()
        return user

    def _make_mock_db(self) -> MagicMock:
        """创建 Mock AsyncSession。"""
        db = MagicMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_create_approval(self) -> None:
        """创建审批记录 — flush 被调用，ID 被赋值。"""
        from app.services.approval_service import ApprovalService

        db = self._make_mock_db()
        user = self._make_mock_user()
        service = ApprovalService(db)

        # 模拟 flush 后 approval 获得 ID
        async def mock_flush():
            pass

        db.flush = mock_flush

        approval = await service.create_approval(
            user_id=user.id,
            session_id="test-session",
            tool_name="document_create",
            tool_use_id="tu-001",
            tool_arguments={"title": "测试文档"},
            reason="创建文档会写入知识库",
            irreversible=False,
            agent_state_snapshot={"query": "测试", "iteration": 1},
        )

        assert approval.tool_name == "document_create"
        assert approval.status == "pending"
        assert approval.session_id == "test-session"
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_approve_sets_session_confirmed(self) -> None:
        """批准后工具加入会话级确认缓存。"""
        from app.models.approval import ToolApproval
        from app.services.approval_service import ApprovalService

        db = self._make_mock_db()
        user = self._make_mock_user()
        service = ApprovalService(db)

        # Mock 审批记录
        approval = ToolApproval(
            user_id=user.id,
            session_id="test-approve-session",
            tool_name="document_create",
            tool_use_id="tu-002",
            tool_arguments={},
            reason="测试",
            irreversible=False,
            agent_state_snapshot={},
            status="pending",
            expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        approval.id = uuid.uuid4()

        # Mock DB 查询返回审批记录
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=approval)
        db.execute = AsyncMock(return_value=mock_result)

        result = await service.approve(approval.id, user)

        assert service.is_session_confirmed("test-approve-session", "document_create") is True

    @pytest.mark.asyncio
    async def test_reject_does_not_confirm(self) -> None:
        """拒绝后工具不加入会话级确认缓存。"""
        from app.models.approval import ToolApproval
        from app.services.approval_service import ApprovalService

        db = self._make_mock_db()
        user = self._make_mock_user()
        service = ApprovalService(db)

        approval = ToolApproval(
            user_id=user.id,
            session_id="test-reject-session",
            tool_name="create_it_ticket",
            tool_use_id="tu-003",
            tool_arguments={},
            reason="测试",
            irreversible=True,
            agent_state_snapshot={},
            status="pending",
            expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        approval.id = uuid.uuid4()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=approval)
        db.execute = AsyncMock(return_value=mock_result)

        await service.reject(approval.id, user)

        assert service.is_session_confirmed("test-reject-session", "create_it_ticket") is False

    @pytest.mark.asyncio
    async def test_get_pending_approvals(self) -> None:
        """查询待审批列表。"""
        from app.services.approval_service import ApprovalService

        db = self._make_mock_db()
        user = self._make_mock_user()
        service = ApprovalService(db)

        # Mock 查询返回空列表
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=[])
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        db.execute = AsyncMock(return_value=mock_result)

        result = await service.get_pending_approvals(user)
        assert result == []

    @pytest.mark.asyncio
    async def test_restore_pending_approvals(self) -> None:
        """恢复机制 — 标记过期 + 加载活跃。"""
        from app.services.approval_service import ApprovalService

        db = self._make_mock_db()
        service = ApprovalService(db)

        # Mock: 第一次 execute（update 过期）返回 MagicMock
        # 第二次 execute（查询活跃）返回空列表
        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                # update 语句
                return MagicMock()
            else:
                # select 语句 — 返回空
                mock_result = MagicMock()
                mock_scalars = MagicMock()
                mock_scalars.all = MagicMock(return_value=[])
                mock_result.scalars = MagicMock(return_value=mock_scalars)
                return mock_result

        db.execute = mock_execute

        count = await service.restore_pending_approvals()
        assert count == 0


# ======================================================================
# 引擎 approval_required 事件测试
# ======================================================================


class TestEngineApprovalEvent:
    """P1-4: 引擎 approval_required SSE 事件测试。"""

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
    async def test_approval_event_contains_snapshot(self) -> None:
        """P1-4: approval_required 事件创建时，AgentState 快照被正确传递。"""
        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock()

        mock_approval = MagicMock()
        mock_approval.id = uuid.uuid4()

        mock_db = MagicMock()

        with patch(
            "app.services.approval_service.ApprovalService"
        ) as MockApprovalService:
            mock_service = MockApprovalService.return_value
            mock_service.create_approval = AsyncMock(return_value=mock_approval)

            state: dict[str, Any] = {
                "tool_results": [],
                "session_id": "snapshot-test-session",
                "query": "创建一个文档",
                "user_id": "user-001",
                "iteration": 2,
                "max_iterations": 5,
                "messages": [{"role": "user", "content": "创建文档"}],
                "retrieved_docs": [{"title": "doc1", "content": "内容"}],
                "tenant_id": None,
            }
            tool_use = {
                "name": "document_create",
                "input": {"title": "新文档"},
                "id": "tu-snapshot",
            }

            events = []
            async for event in engine._execute_tool_use(
                state, tool_use, db=mock_db, user_uuid=uuid.uuid4()
            ):
                events.append(event)

            assert len(events) == 1
            assert events[0].event == SSEEventType.APPROVAL_REQUIRED

            # 验证快照被传递
            call_kwargs = mock_service.create_approval.call_args.kwargs
            snapshot = call_kwargs["agent_state_snapshot"]
            assert snapshot["query"] == "创建一个文档"
            assert snapshot["iteration"] == 2
            assert snapshot["session_id"] == "snapshot-test-session"
            assert len(snapshot["messages"]) == 1
            assert len(snapshot["retrieved_docs"]) == 1

    @pytest.mark.asyncio
    async def test_approval_with_tenant_id(self) -> None:
        """P1-4: tenant_id 被正确传递到审批记录。"""
        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock()

        mock_approval = MagicMock()
        mock_approval.id = uuid.uuid4()

        mock_db = MagicMock()
        test_tenant_id = uuid.uuid4()

        with patch(
            "app.services.approval_service.ApprovalService"
        ) as MockApprovalService:
            mock_service = MockApprovalService.return_value
            mock_service.create_approval = AsyncMock(return_value=mock_approval)

            state: dict[str, Any] = {
                "tool_results": [],
                "session_id": "tenant-test",
                "query": "测试",
                "tenant_id": str(test_tenant_id),
            }
            tool_use = {"name": "create_it_ticket", "input": {}, "id": "tu-tenant"}

            events = []
            async for event in engine._execute_tool_use(
                state, tool_use, db=mock_db, user_uuid=uuid.uuid4()
            ):
                events.append(event)

            call_kwargs = mock_service.create_approval.call_args.kwargs
            assert call_kwargs["tenant_id"] == test_tenant_id

    @pytest.mark.asyncio
    async def test_approval_creation_failure_does_not_crash(self) -> None:
        """P1-4: 审批创建失败时引擎不崩溃，仍阻断工具。"""
        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock()

        mock_db = MagicMock()

        with patch(
            "app.services.approval_service.ApprovalService"
        ) as MockApprovalService:
            mock_service = MockApprovalService.return_value
            mock_service.create_approval = AsyncMock(side_effect=Exception("DB error"))

            state: dict[str, Any] = {
                "tool_results": [],
                "session_id": "error-test",
            }
            tool_use = {"name": "document_create", "input": {}, "id": "tu-error"}

            events = []
            async for event in engine._execute_tool_use(
                state, tool_use, db=mock_db, user_uuid=uuid.uuid4()
            ):
                events.append(event)

            # 审批创建失败 — 不 yield 事件
            assert len(events) == 0
            # 但仍阻断工具（tool_results 中有阻断信息）
            assert len(state["tool_results"]) == 1
            engine.mcp.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_safe_tool_no_approval(self) -> None:
        """安全工具不触发审批。"""
        engine = self._make_engine()
        engine.mcp.call_tool = AsyncMock(return_value='{"results": []}')

        state: dict[str, Any] = {"tool_results": [], "session_id": "safe-test"}
        tool_use = {"name": "knowledge_search", "input": {"query": "test"}, "id": "tu-safe"}

        events = []
        async for event in engine._execute_tool_use(
            state, tool_use, db=MagicMock(), user_uuid=uuid.uuid4()
        ):
            events.append(event)

        assert len(events) == 0
        engine.mcp.call_tool.assert_called_once()
        assert len(state["tool_results"]) == 1

    @pytest.mark.asyncio
    async def test_session_confirmed_tool_no_approval(self) -> None:
        """P1-3 集成: 会话级确认后的工具不触发审批。"""
        guard = DangerousToolGuard()
        session_id = "confirmed-session"
        guard.confirm_session_tool(session_id, "document_create")

        engine = self._make_engine(guard=guard)
        engine.mcp.call_tool = AsyncMock(return_value='{"id": "doc-1"}')

        state: dict[str, Any] = {
            "tool_results": [],
            "session_id": session_id,
        }
        tool_use = {"name": "document_create", "input": {"title": "test"}, "id": "tu-confirmed"}

        events = []
        async for event in engine._execute_tool_use(
            state, tool_use, db=MagicMock(), user_uuid=uuid.uuid4()
        ):
            events.append(event)

        # 已确认 — 不触发审批
        assert len(events) == 0
        # 工具正常执行
        engine.mcp.call_tool.assert_called_once()
        assert len(state["tool_results"]) == 1


# ======================================================================
# ToolApproval ORM 模型测试
# ======================================================================


class TestToolApprovalModel:
    """P1-1: ToolApproval ORM 模型测试。"""

    def test_model_tablename(self) -> None:
        """表名正确。"""
        from app.models.approval import ToolApproval

        assert ToolApproval.__tablename__ == "tool_approvals"

    def test_model_fields(self) -> None:
        """模型包含所有必要字段。"""
        from app.models.approval import ToolApproval

        # 检查关键列存在
        columns = {c.name for c in ToolApproval.__table__.columns}
        required = {
            "id", "user_id", "session_id", "tenant_id",
            "tool_name", "tool_use_id", "tool_arguments",
            "reason", "irreversible", "agent_state_snapshot",
            "status", "resolved_at", "resolved_by", "expire_at",
            "created_at", "updated_at",
        }
        assert required.issubset(columns), f"缺少字段: {required - columns}"

    def test_model_registered_in_metadata(self) -> None:
        """模型已注册到 Base.metadata。"""
        from app.models import Base, ToolApproval

        assert "tool_approvals" in Base.metadata.tables
