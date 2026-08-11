"""
P2-7 混合恢复测试 — EventLogManager + CheckpointManager 协作。

测试范围：
    1. EventLogManager（依赖真实 PostgreSQL）
       - append / list_after / get_last_seq / get_event_count
       - replay 重放语义（list 字段 extend / 标量字段覆盖）
       - truncate / delete_all 清理
    2. CheckpointManager 混合恢复（save/load_checkpoint_with_event_log）
       - save_checkpoint_with_event_log 记录 _base_seq
       - load_checkpoint_with_event_log 重放后续事件
    3. contextvar 集成（event_log_scope / get_current_event_log）
    4. _append_event_log 集成（trace_node 装饰器内追加事件）
    5. 纯算法测试（replay 不依赖 DB，通过 mock list_after）

DB 不可用时自动跳过 DB 集成测试（参考 conftest 的 db_session fixture）。
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock celery（测试环境未安装）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# DB 可用性检测 — 无 PostgreSQL 时跳过 DB 集成测试
def _db_available() -> bool:
    """检查 PostgreSQL 测试库是否可用。"""
    import asyncio
    import os

    async def _check():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        url = os.environ.get("DATABASE_URL", "")
        if not url:
            return False
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_check())
    except Exception:
        return False


# DB 跳过标记 — 用作 DB 集成测试的装饰器
db_required = pytest.mark.skipif(
    not _db_available(),
    reason="PostgreSQL 测试库不可用（DATABASE_URL 未配置或连接失败）",
)


# ======================================================================
# 纯算法测试 — replay 语义（不依赖 DB）
# ======================================================================


class TestReplayAlgorithm:
    """replay 方法重放语义测试 — 通过 mock list_after 验证算法。

    重放语义（与 LangGraph Annotated[list, operator.add] 对齐）：
    - list 字段（messages/retrieved_docs/tool_results/quarantined_docs/milestones）：
      extend 到 base_state 对应字段
    - 其他字段：覆盖
    """

    @pytest.mark.asyncio
    async def test_replay_extends_list_fields(self) -> None:
        """list 字段应 extend（不覆盖）。"""
        from app.memory.event_log import EventLogManager, EventRecord

        manager = EventLogManager(db=MagicMock())
        # mock list_after 返回 2 个事件
        manager.list_after = AsyncMock(return_value=[])  # type: ignore
        manager.list_after.return_value = [
            EventRecord(
                seq=1, event_type="node_end", node_name="retrieve", iteration=1,
                output_data={"messages": [{"role": "tool", "content": "doc1"}]},
            ),
            EventRecord(
                seq=2, event_type="node_end", node_name="tool_call", iteration=1,
                output_data={"messages": [{"role": "tool", "content": "result1"}]},
            ),
        ]

        base_state = {"messages": [{"role": "user", "content": "q1"}]}
        result = await manager.replay("session-1", base_state, after_seq=0)

        # list 字段 extend
        assert result["messages"] == [
            {"role": "user", "content": "q1"},
            {"role": "tool", "content": "doc1"},
            {"role": "tool", "content": "result1"},
        ]

    @pytest.mark.asyncio
    async def test_replay_overrides_scalar_fields(self) -> None:
        """标量字段应覆盖。"""
        from app.memory.event_log import EventLogManager, EventRecord

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[
            EventRecord(
                seq=1, event_type="node_end", node_name="think", iteration=2,
                output_data={"iteration": 2, "answer": "new answer"},
            ),
        ])  # type: ignore

        base_state = {"iteration": 1, "answer": "old answer", "query": "q1"}
        result = await manager.replay("s1", base_state, after_seq=0)

        assert result["iteration"] == 2  # 覆盖
        assert result["answer"] == "new answer"  # 覆盖
        assert result["query"] == "q1"  # 未在 output 中，保留原值

    @pytest.mark.asyncio
    async def test_replay_does_not_modify_base_state(self) -> None:
        """replay 不应修改调用方传入的 base_state（深拷贝）。"""
        from app.memory.event_log import EventLogManager, EventRecord

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[
            EventRecord(
                seq=1, event_type="node_end", node_name="retrieve", iteration=1,
                output_data={"messages": [{"role": "tool", "content": "new"}]},
            ),
        ])  # type: ignore

        base_state = {"messages": [{"role": "user", "content": "orig"}]}
        original_messages = list(base_state["messages"])
        await manager.replay("s1", base_state, after_seq=0)
        # base_state 未被修改
        assert base_state["messages"] == original_messages

    @pytest.mark.asyncio
    async def test_replay_empty_events(self) -> None:
        """无事件时直接返回 base_state（深拷贝）。"""
        from app.memory.event_log import EventLogManager

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[])  # type: ignore

        base_state = {"query": "q1", "messages": []}
        result = await manager.replay("s1", base_state, after_seq=0)
        assert result == base_state
        assert result is not base_state  # 深拷贝

    @pytest.mark.asyncio
    async def test_replay_with_none_base_state(self) -> None:
        """base_state 为 None 时返回空 dict。"""
        from app.memory.event_log import EventLogManager

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[])  # type: ignore

        result = await manager.replay("s1", None, after_seq=0)
        assert result == {}

    @pytest.mark.asyncio
    async def test_replay_skips_non_dict_output(self) -> None:
        """output_data 为 None 或非 dict 时跳过该事件。"""
        from app.memory.event_log import EventLogManager, EventRecord

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[
            EventRecord(seq=1, event_type="node_end", node_name="think", iteration=1,
                       output_data=None),
            EventRecord(seq=2, event_type="node_end", node_name="think", iteration=1,
                       output_data="not a dict"),  # type: ignore
            EventRecord(seq=3, event_type="node_end", node_name="retrieve", iteration=1,
                       output_data={"messages": [{"role": "tool", "content": "doc"}]}),
        ])  # type: ignore

        base_state = {"messages": []}
        result = await manager.replay("s1", base_state, after_seq=0)
        # 只第 3 个事件生效
        assert result["messages"] == [{"role": "tool", "content": "doc"}]

    @pytest.mark.asyncio
    async def test_replay_list_field_with_non_list_value_overwrites(self) -> None:
        """list 字段收到非 list 值时降级为覆盖（异常情况）。"""
        from app.memory.event_log import EventLogManager, EventRecord

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[
            EventRecord(seq=1, event_type="node_end", node_name="think", iteration=1,
                       output_data={"messages": "not a list"}),  # type: ignore
        ])  # type: ignore

        base_state = {"messages": [{"role": "user", "content": "orig"}]}
        result = await manager.replay("s1", base_state, after_seq=0)
        # 非 list 值落到 list 字段：覆盖
        assert result["messages"] == "not a list"

    @pytest.mark.asyncio
    async def test_replay_list_field_when_base_not_list(self) -> None:
        """base_state 中 list 字段不是 list 时，用新 list 覆盖。"""
        from app.memory.event_log import EventLogManager, EventRecord

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[
            EventRecord(seq=1, event_type="node_end", node_name="retrieve", iteration=1,
                       output_data={"messages": [{"role": "tool", "content": "new"}]}),
        ])  # type: ignore

        base_state = {"messages": None}  # 异常情况
        result = await manager.replay("s1", base_state, after_seq=0)
        assert result["messages"] == [{"role": "tool", "content": "new"}]

    @pytest.mark.asyncio
    async def test_replay_multiple_list_fields_all_extend(self) -> None:
        """所有 list 字段（messages/retrieved_docs/tool_results）都应 extend。"""
        from app.memory.event_log import EventLogManager, EventRecord

        manager = EventLogManager(db=MagicMock())
        manager.list_after = AsyncMock(return_value=[
            EventRecord(seq=1, event_type="node_end", node_name="retrieve", iteration=1,
                       output_data={
                           "messages": [{"role": "tool", "content": "m1"}],
                           "retrieved_docs": [{"id": "d1"}],
                           "tool_results": [{"name": "t1", "result": "r1"}],
                       }),
            EventRecord(seq=2, event_type="node_end", node_name="tool_call", iteration=1,
                       output_data={
                           "messages": [{"role": "tool", "content": "m2"}],
                           "tool_results": [{"name": "t2", "result": "r2"}],
                       }),
        ])  # type: ignore

        base_state = {
            "messages": [{"role": "user", "content": "q"}],
            "retrieved_docs": [],
            "tool_results": [],
        }
        result = await manager.replay("s1", base_state, after_seq=0)
        assert len(result["messages"]) == 3  # 1 + 1 + 1
        assert len(result["retrieved_docs"]) == 1
        assert len(result["tool_results"]) == 2


# ======================================================================
# EventRecord 数据类
# ======================================================================


class TestEventRecord:
    """EventRecord 数据类基础测试。"""

    def test_default_values(self) -> None:
        from app.memory.event_log import EventRecord

        rec = EventRecord(seq=1, event_type="node_end", node_name="think", iteration=1)
        assert rec.seq == 1
        assert rec.event_type == "node_end"
        assert rec.node_name == "think"
        assert rec.iteration == 1
        assert rec.input_data is None
        assert rec.output_data is None
        assert rec.metadata == {}
        assert rec.created_at == ""

    def test_with_all_fields(self) -> None:
        from app.memory.event_log import EventRecord

        rec = EventRecord(
            seq=5, event_type="node_end", node_name="retrieve", iteration=2,
            input_data={"query": "q"},
            output_data={"messages": []},
            metadata={"latency_ms": 100.0},
            created_at="2026-08-11T10:00:00Z",
        )
        assert rec.seq == 5
        assert rec.input_data == {"query": "q"}
        assert rec.metadata["latency_ms"] == 100.0


# ======================================================================
# contextvar 集成
# ======================================================================


class TestContextVarIntegration:
    """event_log_scope / get_current_event_log 测试。"""

    def test_no_manager_by_default(self) -> None:
        from app.memory.event_log import get_current_event_log

        assert get_current_event_log() is None

    def test_manager_active_in_scope(self) -> None:
        from app.memory.event_log import EventLogManager, event_log_scope, get_current_event_log

        manager = EventLogManager(db=MagicMock())
        with event_log_scope(manager):
            assert get_current_event_log() is manager

    def test_manager_cleared_after_scope(self) -> None:
        from app.memory.event_log import EventLogManager, event_log_scope, get_current_event_log

        manager = EventLogManager(db=MagicMock())
        with event_log_scope(manager):
            pass
        assert get_current_event_log() is None

    def test_nested_scopes_isolated(self) -> None:
        from app.memory.event_log import EventLogManager, event_log_scope, get_current_event_log

        outer = EventLogManager(db=MagicMock())
        inner = EventLogManager(db=MagicMock())
        with event_log_scope(outer):
            assert get_current_event_log() is outer
            with event_log_scope(inner):
                assert get_current_event_log() is inner
            assert get_current_event_log() is outer

    def test_none_manager_in_scope(self) -> None:
        """scope(None) 时 get_current_event_log 返回 None。"""
        from app.memory.event_log import event_log_scope, get_current_event_log

        with event_log_scope(None):
            assert get_current_event_log() is None


# ======================================================================
# _append_event_log 集成测试（trace_node 装饰器内调用）
# ======================================================================


class TestAppendEventLogInTracer:
    """_append_event_log 在 trace_node 装饰器内的行为测试。"""

    @pytest.mark.asyncio
    async def test_skip_when_no_session_id(self) -> None:
        """session_id 为空时跳过（不调用 manager）。"""
        from app.observability.langfuse_tracer import _append_event_log

        # 不应抛异常
        await _append_event_log(
            session_id="",
            node_name="think",
            iteration=1,
            error=None,
            latency_ms=100.0,
            state={},
            result={"messages": []},
        )

    @pytest.mark.asyncio
    async def test_skip_when_no_manager_in_context(self) -> None:
        """contextvar 未注入 EventLogManager 时跳过。"""
        from app.observability.langfuse_tracer import _append_event_log

        # 不应抛异常
        await _append_event_log(
            session_id="s1",
            node_name="think",
            iteration=1,
            error=None,
            latency_ms=100.0,
            state={},
            result={"messages": []},
        )

    @pytest.mark.asyncio
    async def test_appends_dict_result_as_output_data(self) -> None:
        """节点返回 dict 时作为 output_data。"""
        from app.memory.event_log import EventLogManager, event_log_scope
        from app.observability.langfuse_tracer import _append_event_log

        manager = EventLogManager(db=MagicMock())
        # 让 append 返回 awaitable
        async def _mock_append(**kwargs):
            return 1
        manager.append = _mock_append  # type: ignore

        with event_log_scope(manager):
            await _append_event_log(
                session_id="s1",
                node_name="retrieve",
                iteration=1,
                error=None,
                latency_ms=50.0,
                state={"retrieved_docs": [{"id": "d1"}]},
                result={"messages": [{"role": "tool", "content": "doc"}]},
            )

    @pytest.mark.asyncio
    async def test_appends_non_dict_result_as_preview(self) -> None:
        """节点返回非 dict（str/None）时仅保存 result_preview。"""
        from app.memory.event_log import EventLogManager, event_log_scope
        from app.observability.langfuse_tracer import _append_event_log

        captured: dict = {}

        manager = EventLogManager(db=MagicMock())
        async def _mock_append(**kwargs):
            captured.update(kwargs)
            return 1
        manager.append = _mock_append  # type: ignore

        with event_log_scope(manager):
            await _append_event_log(
                session_id="s1",
                node_name="think",
                iteration=1,
                error=None,
                latency_ms=30.0,
                state={},
                result="route_decision",  # 非 dict
            )
        assert captured["output_data"] == {"result_preview": "route_decision"}
        assert captured["event_type"] == "node_end"
        assert captured["node_name"] == "think"

    @pytest.mark.asyncio
    async def test_appends_error_on_exception(self) -> None:
        """节点抛异常时 output_data 含 error。"""
        from app.memory.event_log import EventLogManager, event_log_scope
        from app.observability.langfuse_tracer import _append_event_log

        captured: dict = {}
        manager = EventLogManager(db=MagicMock())
        async def _mock_append(**kwargs):
            captured.update(kwargs)
            return 1
        manager.append = _mock_append  # type: ignore

        with event_log_scope(manager):
            await _append_event_log(
                session_id="s1",
                node_name="generate",
                iteration=2,
                error="LLM timeout",
                latency_ms=5000.0,
                state={},
                result=None,
            )
        assert captured["output_data"] == {"error": "LLM timeout"}
        assert captured["metadata"]["error"] == "LLM timeout"
        assert captured["metadata"]["latency_ms"] == 5000.0

    @pytest.mark.asyncio
    async def test_swallows_append_exception(self) -> None:
        """manager.append 抛异常时 _append_event_log 不应抛出（不阻塞主流程）。"""
        from app.memory.event_log import EventLogManager, event_log_scope
        from app.observability.langfuse_tracer import _append_event_log

        manager = EventLogManager(db=MagicMock())
        async def _raise(**kwargs):
            raise RuntimeError("DB down")
        manager.append = _raise  # type: ignore

        with event_log_scope(manager):
            # 不应抛异常
            await _append_event_log(
                session_id="s1",
                node_name="think",
                iteration=1,
                error=None,
                latency_ms=100.0,
                state={},
                result={"messages": []},
            )


# ======================================================================
# trace_node 装饰器端到端集成 — 事件日志记录
# ======================================================================


class TestTraceNodeEventLogIntegration:
    """trace_node 装饰器执行后追加事件日志的端到端测试。"""

    @pytest.mark.asyncio
    async def test_trace_node_appends_event_log(self) -> None:
        """trace_node 装饰的节点执行后自动追加事件日志。"""
        from app.memory.event_log import EventLogManager, event_log_scope
        from app.observability.langfuse_tracer import trace_node

        manager = EventLogManager(db=MagicMock())
        captured: list[dict] = []
        async def _capture_append(**kwargs):
            captured.append(kwargs)
            return len(captured)
        manager.append = _capture_append  # type: ignore

        class FakeEngine:
            _trace_ctx = None

            @trace_node("think")
            async def _think(self, state: dict) -> dict:
                return {"messages": [{"role": "tool", "content": "decision"}]}

        engine = FakeEngine()
        with event_log_scope(manager):
            await engine._think({"iteration": 1, "session_id": "s1"})

        # 验证事件被追加
        assert len(captured) == 1
        assert captured[0]["session_id"] == "s1"
        assert captured[0]["node_name"] == "think"
        assert captured[0]["event_type"] == "node_end"
        assert captured[0]["output_data"] == {"messages": [{"role": "tool", "content": "decision"}]}
        assert captured[0]["iteration"] == 1

    @pytest.mark.asyncio
    async def test_trace_node_no_event_log_when_no_manager(self) -> None:
        """无 EventLogManager 时 trace_node 仍正常工作（仅不记事件）。"""
        from app.observability.langfuse_tracer import trace_node

        class FakeEngine:
            _trace_ctx = None

            @trace_node("retrieve")
            async def _retrieve(self, state: dict) -> dict:
                return {"retrieved_docs": [{"id": "d1"}]}

        engine = FakeEngine()
        # 不应抛异常
        result = await engine._retrieve({"iteration": 1, "session_id": "s1"})
        assert result == {"retrieved_docs": [{"id": "d1"}]}


# ======================================================================
# DB 集成测试 — 依赖真实 PostgreSQL（db_session fixture）
# ======================================================================


@db_required
@pytest.mark.asyncio
async def test_db_append_and_list_after(db_session) -> None:
    """DB 集成：append 后能 list_after 查询到。"""
    from app.memory.event_log import EventLogManager

    manager = EventLogManager(db_session)
    seq1 = await manager.append(
        session_id="test-session-1",
        event_type="node_end",
        node_name="think",
        output_data={"messages": [{"role": "tool", "content": "m1"}]},
        iteration=1,
    )
    assert seq1 >= 1

    seq2 = await manager.append(
        session_id="test-session-1",
        event_type="node_end",
        node_name="retrieve",
        output_data={"messages": [{"role": "tool", "content": "m2"}]},
        iteration=1,
    )
    assert seq2 == seq1 + 1

    events = await manager.list_after("test-session-1", after_seq=0)
    assert len(events) == 2
    assert events[0].seq == seq1
    assert events[0].node_name == "think"
    assert events[1].seq == seq2
    assert events[1].node_name == "retrieve"


@db_required
@pytest.mark.asyncio
async def test_db_get_last_seq(db_session) -> None:
    """DB 集成：get_last_seq 返回最新 seq。"""
    from app.memory.event_log import EventLogManager

    manager = EventLogManager(db_session)
    assert await manager.get_last_seq("test-session-2") == 0

    await manager.append(
        session_id="test-session-2",
        event_type="node_end",
        node_name="think",
        output_data={},
        iteration=1,
    )
    seq = await manager.get_last_seq("test-session-2")
    assert seq == 1


@db_required
@pytest.mark.asyncio
async def test_db_replay_with_real_events(db_session) -> None:
    """DB 集成：append 多个事件后 replay 重放。"""
    from app.memory.event_log import EventLogManager

    manager = EventLogManager(db_session)
    session_id = "test-session-replay"

    # 事件 1: retrieve
    await manager.append(
        session_id=session_id,
        event_type="node_end",
        node_name="retrieve",
        output_data={
            "messages": [{"role": "tool", "content": "doc1"}],
            "retrieved_docs": [{"id": "d1"}],
        },
        iteration=1,
    )
    # 事件 2: tool_call
    await manager.append(
        session_id=session_id,
        event_type="node_end",
        node_name="tool_call",
        output_data={
            "messages": [{"role": "tool", "content": "result1"}],
            "tool_results": [{"name": "search", "result": "ok"}],
        },
        iteration=1,
    )
    # 事件 3: think
    await manager.append(
        session_id=session_id,
        event_type="node_end",
        node_name="think",
        output_data={"iteration": 2},
        iteration=2,
    )

    # 重放：从空 state 开始
    result = await manager.replay(session_id, base_state={}, after_seq=0)
    assert len(result["messages"]) == 2
    assert len(result["retrieved_docs"]) == 1
    assert len(result["tool_results"]) == 1
    assert result["iteration"] == 2


@db_required
@pytest.mark.asyncio
async def test_db_replay_from_checkpoint(db_session) -> None:
    """DB 集成：从 base_seq 之后重放（混合恢复核心场景）。"""
    from app.memory.event_log import EventLogManager

    manager = EventLogManager(db_session)
    session_id = "test-session-cp"

    # 模拟 Checkpoint 之前的 2 个事件
    await manager.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={"messages": [{"role": "user", "content": "q1"}]}, iteration=1,
    )
    cp_seq = await manager.append(
        session_id=session_id, event_type="node_end", node_name="retrieve",
        output_data={"messages": [{"role": "tool", "content": "doc1"}], "retrieved_docs": [{"id": "d1"}]},
        iteration=1,
    )

    # 模拟 Checkpoint 之后又执行了 2 个事件
    await manager.append(
        session_id=session_id, event_type="node_end", node_name="tool_call",
        output_data={"messages": [{"role": "tool", "content": "result1"}], "tool_results": [{"name": "t1"}]},
        iteration=1,
    )
    await manager.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={"iteration": 2}, iteration=2,
    )

    # 混合恢复：从 Checkpoint 状态（含到 cp_seq 为止的所有事件效果）+ 重放后续
    checkpoint_state = {
        "messages": [
            {"role": "user", "content": "q1"},
            {"role": "tool", "content": "doc1"},
        ],
        "retrieved_docs": [{"id": "d1"}],
    }
    final_state = await manager.replay(session_id, checkpoint_state, after_seq=cp_seq)

    # 验证重放追加了 Checkpoint 之后的 2 个事件
    assert len(final_state["messages"]) == 3  # 2 + 1 (tool_call 的 messages)
    assert final_state["messages"][-1] == {"role": "tool", "content": "result1"}
    assert len(final_state["tool_results"]) == 1
    assert final_state["iteration"] == 2


@db_required
@pytest.mark.asyncio
async def test_db_truncate(db_session) -> None:
    """DB 集成：truncate 保留最近 N 条。"""
    from app.memory.event_log import EventLogManager

    manager = EventLogManager(db_session)
    session_id = "test-session-trunc"

    for i in range(5):
        await manager.append(
            session_id=session_id, event_type="node_end", node_name="think",
            output_data={"i": i}, iteration=i,
        )

    # 保留最近 2 条
    deleted = await manager.truncate(session_id, keep_last_n=2)
    assert deleted >= 3  # 至少删除 3 条（前 3 条）

    events = await manager.list_after(session_id, after_seq=0)
    assert len(events) == 2


@db_required
@pytest.mark.asyncio
async def test_db_delete_all(db_session) -> None:
    """DB 集成：delete_all 清空会话全部事件。"""
    from app.memory.event_log import EventLogManager

    manager = EventLogManager(db_session)
    session_id = "test-session-del"

    await manager.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={}, iteration=1,
    )
    await manager.append(
        session_id=session_id, event_type="node_end", node_name="retrieve",
        output_data={}, iteration=1,
    )

    deleted = await manager.delete_all(session_id)
    assert deleted == 2
    assert await manager.get_last_seq(session_id) == 0


@db_required
@pytest.mark.asyncio
async def test_db_get_event_count(db_session) -> None:
    """DB 集成：get_event_count 返回事件总数。"""
    from app.memory.event_log import EventLogManager

    manager = EventLogManager(db_session)
    session_id = "test-session-count"

    assert await manager.get_event_count(session_id) == 0
    await manager.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={}, iteration=1,
    )
    await manager.append(
        session_id=session_id, event_type="node_end", node_name="retrieve",
        output_data={}, iteration=1,
    )
    assert await manager.get_event_count(session_id) == 2


# ======================================================================
# CheckpointManager 混合恢复集成测试
# ======================================================================


@db_required
@pytest.mark.asyncio
async def test_checkpoint_save_with_event_log(db_session) -> None:
    """CheckpointManager.save_checkpoint_with_event_log 记录 _base_seq。"""
    from app.memory.checkpoint import CheckpointManager
    from app.memory.event_log import EventLogManager

    session_id = "test-cp-el-1"

    # 先追加 2 个事件
    event_log = EventLogManager(db_session)
    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={"messages": [{"role": "user", "content": "q1"}]}, iteration=1,
    )
    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="retrieve",
        output_data={"messages": [{"role": "tool", "content": "doc1"}]}, iteration=1,
    )

    # 保存 Checkpoint（自动取最新 seq 作为 base_seq）
    cp_manager = CheckpointManager(db_session)
    state = {"messages": [{"role": "user", "content": "q1"}, {"role": "tool", "content": "doc1"}]}
    await cp_manager.save_checkpoint_with_event_log(session_id, state, iteration=1)

    # 验证 _base_seq 已嵌入
    loaded = await cp_manager.load_checkpoint(session_id)
    assert loaded is not None
    assert loaded["_base_seq"] == 2  # 2 个事件


@db_required
@pytest.mark.asyncio
async def test_checkpoint_load_with_event_log_replay(db_session) -> None:
    """CheckpointManager.load_checkpoint_with_event_log 重放后续事件。"""
    from app.memory.checkpoint import CheckpointManager
    from app.memory.event_log import EventLogManager

    session_id = "test-cp-el-2"
    event_log = EventLogManager(db_session)
    cp_manager = CheckpointManager(db_session)

    # 阶段 1: 追加 2 个事件 + 保存 Checkpoint
    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={"messages": [{"role": "user", "content": "q1"}]}, iteration=1,
    )
    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="retrieve",
        output_data={"messages": [{"role": "tool", "content": "doc1"}], "retrieved_docs": [{"id": "d1"}]},
        iteration=1,
    )
    state_at_checkpoint = {
        "messages": [{"role": "user", "content": "q1"}, {"role": "tool", "content": "doc1"}],
        "retrieved_docs": [{"id": "d1"}],
    }
    await cp_manager.save_checkpoint_with_event_log(session_id, state_at_checkpoint, iteration=1)

    # 阶段 2: Checkpoint 之后又执行了 1 个事件（未存 Checkpoint）
    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="tool_call",
        output_data={"messages": [{"role": "tool", "content": "result1"}], "tool_results": [{"name": "t1"}]},
        iteration=1,
    )

    # 阶段 3: 混合恢复 — 加载 Checkpoint + 重放后续事件
    final_state = await cp_manager.load_checkpoint_with_event_log(session_id)
    assert final_state is not None

    # 验证重放了 tool_call 事件
    messages = final_state.get("messages", [])
    assert any(m.get("content") == "result1" for m in messages)
    assert len(final_state.get("tool_results", [])) == 1

    # _base_seq 不应出现在最终状态中（已被 load 时 pop）
    assert "_base_seq" not in final_state


@db_required
@pytest.mark.asyncio
async def test_checkpoint_load_no_replay(db_session) -> None:
    """load_checkpoint_with_event_log(replay_events=False) 仅返回 Checkpoint 快照。"""
    from app.memory.checkpoint import CheckpointManager
    from app.memory.event_log import EventLogManager

    session_id = "test-cp-el-3"
    event_log = EventLogManager(db_session)
    cp_manager = CheckpointManager(db_session)

    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={"messages": [{"role": "user", "content": "q1"}]}, iteration=1,
    )
    await cp_manager.save_checkpoint_with_event_log(
        session_id, {"messages": [{"role": "user", "content": "q1"}]}, iteration=1
    )

    # 阶段 2: 追加事件但未存 Checkpoint
    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="retrieve",
        output_data={"messages": [{"role": "tool", "content": "doc1"}]}, iteration=1,
    )

    # 不重放 — 仅返回 Checkpoint 快照
    snapshot = await cp_manager.load_checkpoint_with_event_log(session_id, replay_events=False)
    assert snapshot is not None
    # 快照中 messages 不含 retrieve 事件
    messages = snapshot.get("messages", [])
    assert all(m.get("content") != "doc1" for m in messages)


@db_required
@pytest.mark.asyncio
async def test_checkpoint_load_no_new_events(db_session) -> None:
    """Checkpoint 之后无新事件时直接返回快照状态。"""
    from app.memory.checkpoint import CheckpointManager
    from app.memory.event_log import EventLogManager

    session_id = "test-cp-el-4"
    event_log = EventLogManager(db_session)
    cp_manager = CheckpointManager(db_session)

    await event_log.append(
        session_id=session_id, event_type="node_end", node_name="think",
        output_data={"messages": [{"role": "user", "content": "q1"}]}, iteration=1,
    )
    await cp_manager.save_checkpoint_with_event_log(
        session_id, {"messages": [{"role": "user", "content": "q1"}]}, iteration=1
    )

    # 不追加新事件，直接加载
    state = await cp_manager.load_checkpoint_with_event_log(session_id)
    assert state is not None
    # 状态等于 Checkpoint 快照（无新事件可重放）
    assert "_base_seq" not in state
