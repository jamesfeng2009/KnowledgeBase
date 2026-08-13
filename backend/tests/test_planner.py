"""
显式计划管理器测试 — P1-9：plan 状态清单 + 偏离检测 + 仅重规划剩余。

覆盖：
    1. build_initial_plan：LLM JSON 解析 / 非法动作过滤 / 失败降级默认计划；
    2. 状态推进：mark_action_done / pending_steps / format_plan_brief；
    3. assess_deviation：偏离度解析、范围钳制、LLM 失败保守返回 0；
    4. replan_remaining：保留 done、替换 pending、强制 generate 收尾；
    5. engine 接线：初始计划注入循环、think 计划视图、_maybe_replan 触发与上限。

不依赖真实 LLM API — 全部使用 Mock。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.planner import (
    DEVIATION_THRESHOLD,
    STEP_DONE,
    STEP_PENDING,
    PlanManager,
    map_task_type_to_action,
)


# ======================================================================
# Mock LLM
# ======================================================================


class QueueLLM:
    """Mock LLM — 按队列依次返回预设响应。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: int = 0
        self.last_messages: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        self.last_messages = list(messages)
        yield self._responses[idx]


class ErrorLLM:
    """Mock LLM — 调用即抛异常。"""

    async def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        raise RuntimeError("LLM unavailable")
        yield  # noqa: E701


def _sample_plan() -> list[dict[str, Any]]:
    return [
        {"step_id": 1, "action": "retrieve", "description": "检索文档", "status": STEP_PENDING},
        {"step_id": 2, "action": "tool_call", "description": "查询工单", "status": STEP_PENDING},
        {"step_id": 3, "action": "generate", "description": "生成答案", "status": STEP_PENDING},
    ]


# ======================================================================
# 1. 初始计划生成
# ======================================================================


class TestBuildInitialPlan:
    """初始计划生成测试。"""

    @pytest.mark.asyncio
    async def test_parse_llm_plan(self) -> None:
        llm = QueueLLM([
            '[{"action": "retrieve", "description": "检索政策"},'
            ' {"action": "generate", "description": "生成答案"}]'
        ])
        planner = PlanManager(llm)

        plan = await planner.build_initial_plan("报销政策是什么")

        assert len(plan) == 2
        assert plan[0]["action"] == "retrieve"
        assert plan[0]["status"] == STEP_PENDING
        assert plan[1]["action"] == "generate"
        assert plan[0]["step_id"] == 1
        assert plan[1]["step_id"] == 2

    @pytest.mark.asyncio
    async def test_invalid_actions_filtered(self) -> None:
        llm = QueueLLM([
            '[{"action": "retrieve", "description": "检索"},'
            ' {"action": "hack", "description": "非法动作"},'
            ' {"action": "generate", "description": "生成"}]'
        ])
        planner = PlanManager(llm)

        plan = await planner.build_initial_plan("q")

        assert [s["action"] for s in plan] == ["retrieve", "generate"]

    @pytest.mark.asyncio
    async def test_markdown_wrapped_json(self) -> None:
        llm = QueueLLM([
            '```json\n[{"action": "retrieve", "description": "检索"},'
            ' {"action": "generate", "description": "生成"}]\n```'
        ])
        planner = PlanManager(llm)

        plan = await planner.build_initial_plan("q")

        assert len(plan) == 2

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back(self) -> None:
        llm = QueueLLM(["这不是 JSON"])
        planner = PlanManager(llm)

        plan = await planner.build_initial_plan("q")

        # 降级默认两步计划
        assert [s["action"] for s in plan] == ["retrieve", "generate"]

    @pytest.mark.asyncio
    async def test_llm_error_falls_back(self) -> None:
        planner = PlanManager(ErrorLLM())

        plan = await planner.build_initial_plan("q")

        assert [s["action"] for s in plan] == ["retrieve", "generate"]


# ======================================================================
# 2. 状态推进
# ======================================================================


class TestPlanStateTransitions:
    """计划状态推进测试。"""

    def test_mark_action_done_marks_first_pending(self) -> None:
        plan = _sample_plan()

        marked = PlanManager.mark_action_done(plan, "retrieve")

        assert marked is not None
        assert marked["step_id"] == 1
        assert plan[0]["status"] == STEP_DONE
        assert plan[1]["status"] == STEP_PENDING

    def test_mark_action_done_no_match(self) -> None:
        plan = _sample_plan()

        assert PlanManager.mark_action_done(plan, "unknown_action") is None

    def test_pending_and_done_steps(self) -> None:
        plan = _sample_plan()
        PlanManager.mark_action_done(plan, "retrieve")

        assert len(PlanManager.done_steps(plan)) == 1
        assert len(PlanManager.pending_steps(plan)) == 2

    def test_format_plan_brief(self) -> None:
        plan = _sample_plan()
        PlanManager.mark_action_done(plan, "retrieve")

        brief = PlanManager.format_plan_brief(plan)

        assert "已完成[1.检索文档]" in brief
        assert "剩余[2.查询工单；3.生成答案]" in brief

    def test_format_plan_brief_empty(self) -> None:
        assert PlanManager.format_plan_brief([]) == ""


# ======================================================================
# 3. 偏离检测
# ======================================================================


class TestAssessDeviation:
    """偏离度判定测试。"""

    @pytest.mark.asyncio
    async def test_parse_deviation(self) -> None:
        llm = QueueLLM(['{"deviation": 0.8, "reason": "检索为空"}'])
        planner = PlanManager(llm)

        score = await planner.assess_deviation("q", _sample_plan(), "检索到 0 篇文档")

        assert score == 0.8

    @pytest.mark.asyncio
    async def test_deviation_clamped_to_range(self) -> None:
        llm = QueueLLM(['{"deviation": 1.7, "reason": "越界"}'])
        planner = PlanManager(llm)

        score = await planner.assess_deviation("q", _sample_plan(), "obs")

        assert score == 1.0

    @pytest.mark.asyncio
    async def test_llm_error_returns_zero(self) -> None:
        """LLM 失败保守返回 0 — 不触发重规划。"""
        planner = PlanManager(ErrorLLM())

        score = await planner.assess_deviation("q", _sample_plan(), "obs")

        assert score == 0.0

    @pytest.mark.asyncio
    async def test_invalid_json_returns_zero(self) -> None:
        llm = QueueLLM(["无法解析"])
        planner = PlanManager(llm)

        assert await planner.assess_deviation("q", _sample_plan(), "obs") == 0.0


# ======================================================================
# 4. 仅重规划剩余
# ======================================================================


class TestReplanRemaining:
    """剩余步骤重规划测试。"""

    @pytest.mark.asyncio
    async def test_done_steps_preserved(self) -> None:
        plan = _sample_plan()
        PlanManager.mark_action_done(plan, "retrieve")

        llm = QueueLLM([
            '[{"action": "tool_call", "description": "改查实时系统"},'
            ' {"action": "generate", "description": "生成答案"}]'
        ])
        planner = PlanManager(llm)

        new_plan = await planner.replan_remaining("q", plan, "检索为空")

        # done 步骤保留在前
        assert new_plan[0]["status"] == STEP_DONE
        assert new_plan[0]["action"] == "retrieve"
        # 新 pending 步骤接续编号
        assert new_plan[1]["step_id"] == 2
        assert new_plan[1]["description"] == "改查实时系统"
        assert new_plan[1]["status"] == STEP_PENDING

    @pytest.mark.asyncio
    async def test_generate_forced_as_last_step(self) -> None:
        llm = QueueLLM(['[{"action": "tool_call", "description": "查系统"}]'])
        planner = PlanManager(llm)

        new_plan = await planner.replan_remaining("q", _sample_plan(), "obs")

        assert new_plan[-1]["action"] == "generate"

    @pytest.mark.asyncio
    async def test_llm_error_returns_original(self) -> None:
        plan = _sample_plan()
        planner = PlanManager(ErrorLLM())

        new_plan = await planner.replan_remaining("q", plan, "obs")

        assert new_plan is plan


# ======================================================================
# 5. crew 对齐映射
# ======================================================================


class TestCrewAlignment:
    """crew 子任务类型 → plan 动作映射测试。"""

    def test_mapping(self) -> None:
        assert map_task_type_to_action("qa") == "retrieve"
        assert map_task_type_to_action("workflow") == "tool_call"
        assert map_task_type_to_action("action") == "tool_call"
        assert map_task_type_to_action("unknown") == "retrieve"


# ======================================================================
# 6. engine 接线
# ======================================================================


class TestEnginePlanWiring:
    """engine plan 接线测试。"""

    def _make_engine(self, llm_response: str = "generate", planner: Any = None):
        from app.rag.engine import AgenticRAGEngine
        from tests.test_rag_engine import (
            FakeGenerator,
            FakeLLM,
            FakeMCPClient,
            FakeReranker,
            FakeRetriever,
        )

        engine = AgenticRAGEngine(
            llm=FakeLLM(llm_response),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
            planner=planner,
        )
        return engine

    def _make_state(self, **overrides: Any) -> dict[str, Any]:
        state: dict[str, Any] = {
            "query": "报销流程怎么走",
            "user_id": "user-001",
            "session_id": "session-001",
            "messages": [],
            "retrieved_docs": [],
            "tool_results": [],
            "answer": "",
            "iteration": 0,
            "max_iterations": 5,
            "kb_ids": None,
            "memory_context": "",
        }
        state.update(overrides)
        return state

    @pytest.mark.asyncio
    async def test_initial_plan_built_in_loop(self) -> None:
        """决策循环开始时生成初始计划（LLM 无法解析时降级默认计划）。"""
        engine = self._make_engine(llm_response="generate")
        state = self._make_state()

        await engine._run_decision_loop(state)

        assert "plan_steps" in state
        # FakeLLM 响应 "generate" 不是 JSON → 降级默认两步计划
        assert [s["action"] for s in state["plan_steps"]] == ["retrieve", "generate"]
        assert state["replan_count"] == 0

    @pytest.mark.asyncio
    async def test_think_injects_plan_brief(self) -> None:
        """think 动态上下文包含计划视图。"""
        llm = QueueLLM(["generate"])
        from app.rag.engine import AgenticRAGEngine
        from tests.test_rag_engine import (
            FakeGenerator,
            FakeMCPClient,
            FakeReranker,
            FakeRetriever,
        )

        engine = AgenticRAGEngine(
            llm=llm,
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )
        state = self._make_state(iteration=1, plan_steps=_sample_plan())

        await engine._think(state)

        dynamic_msg = llm.last_messages[-2]["content"]
        assert "执行计划" in dynamic_msg
        assert "检索文档" in dynamic_msg

    @pytest.mark.asyncio
    async def test_maybe_replan_not_triggered(self) -> None:
        """触发器未命中时零 LLM 调用。"""
        planner = AsyncMock()
        planner.max_replans = 2
        engine = self._make_engine(planner=planner)
        state = self._make_state(plan_steps=_sample_plan(), replan_count=0)

        result = await engine._maybe_replan(state, "obs", trigger=False)

        assert result is False
        planner.assess_deviation.assert_not_called()

    @pytest.mark.asyncio
    async def test_maybe_replan_low_deviation_no_change(self) -> None:
        """偏离度低于阈值时不改动计划。"""
        planner = AsyncMock()
        planner.max_replans = 2
        planner.assess_deviation = AsyncMock(return_value=0.3)
        engine = self._make_engine(planner=planner)
        plan = _sample_plan()
        state = self._make_state(plan_steps=plan, replan_count=0)

        result = await engine._maybe_replan(state, "obs", trigger=True)

        assert result is False
        planner.replan_remaining.assert_not_called()
        assert state["plan_steps"] is plan

    @pytest.mark.asyncio
    async def test_maybe_replan_high_deviation_replans(self) -> None:
        """偏离度超阈值 → 仅重规划剩余，计数 +1。"""
        plan = _sample_plan()
        PlanManager.mark_action_done(plan, "retrieve")
        new_plan = plan[:1] + [
            {"step_id": 2, "action": "tool_call", "description": "改查系统", "status": STEP_PENDING},
            {"step_id": 3, "action": "generate", "description": "生成", "status": STEP_PENDING},
        ]
        planner = AsyncMock()
        planner.max_replans = 2
        planner.assess_deviation = AsyncMock(return_value=DEVIATION_THRESHOLD)
        planner.replan_remaining = AsyncMock(return_value=new_plan)
        engine = self._make_engine(planner=planner)
        state = self._make_state(plan_steps=plan, replan_count=0)

        result = await engine._maybe_replan(state, "检索为空", trigger=True)

        assert result is True
        assert state["plan_steps"] == new_plan
        assert state["replan_count"] == 1

    @pytest.mark.asyncio
    async def test_maybe_replan_respects_cap(self) -> None:
        """达到最大重规划次数后不再重规划。"""
        planner = AsyncMock()
        planner.max_replans = 2
        engine = self._make_engine(planner=planner)
        state = self._make_state(plan_steps=_sample_plan(), replan_count=2)

        result = await engine._maybe_replan(state, "obs", trigger=True)

        assert result is False
        planner.assess_deviation.assert_not_called()

    @pytest.mark.asyncio
    async def test_maybe_replan_no_plan_noop(self) -> None:
        """无计划（降级模式）时不做任何事。"""
        planner = AsyncMock()
        planner.max_replans = 2
        engine = self._make_engine(planner=planner)
        state = self._make_state(plan_steps=[], replan_count=0)

        result = await engine._maybe_replan(state, "obs", trigger=True)

        assert result is False
