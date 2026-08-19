"""Agent 执行轨迹聚合指标测试 — P0-3 轨迹聚合 + P1-7 卡死监控。

覆盖：
- TrailAggregator：完成率/工具命中率/平均收敛步数/兜圈率/超时率计算
- TrailAggregator：工具命中分布、reset、全局单例
- engine 埋点：answer() 结束后轨迹上报到全局聚合器
- P1-7 卡死防护：_think_with_timeout 步骤超时、总任务超时、max_iterations 告警标记
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.observability.trail_aggregator import (
    TrailAggregator,
    get_trail_aggregator,
    reset_trail_aggregator,
)
from app.rag.engine import AgentState, AgenticRAGEngine


# ======================================================================
# TrailAggregator 单元测试
# ======================================================================


class TestTrailAggregator:
    """轨迹聚合器指标计算测试。"""

    @pytest.mark.asyncio
    async def test_normal_session_completion(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(iterations=2)
        summary = await agg.window_summary()
        w = summary["window"]
        assert w["total_sessions"] == 1
        assert w["completion_rate"] == 1.0
        assert w["max_iterations_rate"] == 0.0
        assert w["timeout_rate"] == 0.0
        assert w["avg_iterations"] == 2.0

    @pytest.mark.asyncio
    async def test_max_iterations_session(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(iterations=5, max_iter_reached=True)
        summary = await agg.window_summary()
        w = summary["window"]
        assert w["completion_rate"] == 0.0
        assert w["max_iterations_rate"] == 1.0
        assert w["loitering_rate"] == 1.0  # 兜圈率含 max_iterations

    @pytest.mark.asyncio
    async def test_total_timeout_session(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(iterations=3, total_timeout=True)
        summary = await agg.window_summary()
        w = summary["window"]
        assert w["timeout_rate"] == 1.0
        assert w["completion_rate"] == 0.0
        # 超时不算兜圈（兜圈率只含 dedup + max_iterations）
        assert w["loitering_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_dedup_hit_session_loitering(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(iterations=2, dedup_hit=True)
        summary = await agg.window_summary()
        w = summary["window"]
        assert w["dedup_hit_rate"] == 1.0
        assert w["loitering_rate"] == 1.0  # 兜圈率含 dedup 指针引用

    @pytest.mark.asyncio
    async def test_tool_hit_metrics(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(
            iterations=2,
            tool_calls=[
                {"name": "knowledge_search", "hit": True},
                {"name": "document_get", "hit": False},
            ],
        )
        await agg.record_session(iterations=1)  # 无工具会话
        summary = await agg.window_summary()
        w = summary["window"]
        assert w["tool_call_rate"] == 0.5
        assert w["tool_hit_rate"] == 0.5
        assert summary["tool_distribution"] == {"knowledge_search": 1}

    @pytest.mark.asyncio
    async def test_avg_iterations_completed_only(self) -> None:
        """平均收敛步数只统计正常结束的会话。"""
        agg = TrailAggregator()
        await agg.record_session(iterations=2)
        await agg.record_session(iterations=4)
        await agg.record_session(iterations=5, max_iter_reached=True)
        summary = await agg.window_summary()
        assert summary["window"]["avg_iterations"] == 3.0

    @pytest.mark.asyncio
    async def test_mixed_sessions_rates(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(iterations=2)
        await agg.record_session(iterations=3, dedup_hit=True)
        await agg.record_session(iterations=5, max_iter_reached=True)
        await agg.record_session(iterations=1, total_timeout=True)
        summary = await agg.window_summary()
        w = summary["window"]
        assert w["total_sessions"] == 4
        assert w["completion_rate"] == 0.5
        assert w["max_iterations_rate"] == 0.25
        assert w["timeout_rate"] == 0.25
        assert w["dedup_hit_rate"] == 0.25
        assert w["loitering_rate"] == 0.5

    @pytest.mark.asyncio
    async def test_latency_accumulation(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(
            iterations=1,
            think_latency_ms=100.0,
            retrieve_latency_ms=50.0,
            tool_latency_ms=30.0,
        )
        summary = await agg.window_summary()
        w = summary["window"]
        assert w["avg_think_latency_ms"] == 100.0
        assert w["avg_retrieve_latency_ms"] == 50.0
        assert w["avg_tool_latency_ms"] == 30.0

    @pytest.mark.asyncio
    async def test_reset(self) -> None:
        agg = TrailAggregator()
        await agg.record_session(iterations=2)
        await agg.reset()
        summary = await agg.window_summary()
        assert summary["window"]["total_sessions"] == 0
        assert summary["tool_distribution"] == {}

    @pytest.mark.asyncio
    async def test_global_singleton(self) -> None:
        reset_trail_aggregator()
        agg1 = get_trail_aggregator()
        agg2 = get_trail_aggregator()
        assert agg1 is agg2
        await agg1.record_session(iterations=1)
        summary = await agg2.window_summary()
        assert summary["window"]["total_sessions"] == 1
        reset_trail_aggregator()


# ======================================================================
# P2-9 滑动窗口驱逐
# ======================================================================


class TestSlidingWindow:
    """P2-9: window_seconds 实际生效 —— 超出窗口的会话被驱逐。"""

    @pytest.mark.asyncio
    async def test_evict_removes_expired_sessions(self, monkeypatch) -> None:
        """超出 window_seconds 的会话在 record/window_summary 时被驱逐。"""
        agg = TrailAggregator(window_seconds=100)
        fake_now = [1000.0]
        monkeypatch.setattr(
            "app.observability.trail_aggregator.time.monotonic",
            lambda: fake_now[0],
        )

        # t=1000 上报会话 A
        await agg.record_session(iterations=2)
        # t=1050 上报会话 B（cutoff=950，A@1000 仍在窗口内）
        fake_now[0] = 1050.0
        await agg.record_session(iterations=4)

        summary = await agg.window_summary()
        assert summary["window"]["total_sessions"] == 2
        assert summary["window"]["avg_iterations"] == 3.0  # (2+4)/2

        # 时间推进到 t=1110：cutoff=1010，A@1000 超出窗口被驱逐，B@1050 保留
        fake_now[0] = 1110.0
        summary = await agg.window_summary()
        assert summary["window"]["total_sessions"] == 1
        assert summary["window"]["avg_iterations"] == 4.0

    @pytest.mark.asyncio
    async def test_evict_on_record_session(self, monkeypatch) -> None:
        """record_session 入队前先驱逐过期记录，避免窗口无限增长。"""
        agg = TrailAggregator(window_seconds=100)
        fake_now = [1000.0]
        monkeypatch.setattr(
            "app.observability.trail_aggregator.time.monotonic",
            lambda: fake_now[0],
        )

        await agg.record_session(iterations=2)  # A@1000
        # 推进到远超窗口后上报 B —— A 应在入队前被驱逐
        fake_now[0] = 5000.0
        await agg.record_session(iterations=6)  # B@5000

        summary = await agg.window_summary()
        assert summary["window"]["total_sessions"] == 1
        assert summary["window"]["avg_iterations"] == 6.0


# ======================================================================
# engine 埋点与卡死防护测试
# ======================================================================


class _FakeLLM:
    """Mock LLM — 按预设文本响应。"""

    def __init__(self, response: str = "generate") -> None:
        self.response = response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        yield self.response


class _FakeRetriever:
    async def search(
        self, query: str, kb_ids: list[str] | None = None, top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return []


class _FakeReranker:
    async def rerank(
        self, query: str, documents: list[dict[str, Any]], top_k: int = 5,
    ) -> list[dict[str, Any]]:
        return []


class _FakeGenerator:
    async def generate(
        self,
        query: str,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str = "",
        constraint_context: Any = None,
    ) -> AsyncIterator[str]:
        yield "答案"


class _FakeMCPClient:
    async def get_tools_for_llm(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        return "{}"


def _make_engine(
    llm_response: str = "generate",
    max_iterations: int = 5,
) -> AgenticRAGEngine:
    return AgenticRAGEngine(
        llm=_FakeLLM(llm_response),
        mcp_client=_FakeMCPClient(),
        retriever=_FakeRetriever(),
        reranker=_FakeReranker(),
        generator=_FakeGenerator(),
        cache=None,
        max_iterations=max_iterations,
    )


def _make_state(**overrides: Any) -> AgentState:
    state: AgentState = {
        "query": "测试问题",
        "rewritten_query": None,
        "user_id": "user-1",
        "session_id": "session-1",
        "messages": [],
        "retrieved_docs": [],
        "tool_results": [],
        "answer": "",
        "iteration": 0,
        "max_iterations": 5,
        "kb_ids": None,
        "memory_context": "",
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


class TestEngineTrailRecording:
    """engine 轨迹埋点测试。"""

    @pytest.mark.asyncio
    async def test_answer_records_trajectory(self) -> None:
        """answer() 正常结束后应向全局聚合器上报会话。"""
        reset_trail_aggregator()
        engine = _make_engine(llm_response="generate")

        async for _ in engine.answer("测试", "user-1", "session-trail"):
            pass

        summary = await get_trail_aggregator().window_summary()
        w = summary["window"]
        assert w["total_sessions"] == 1
        assert w["completion_rate"] == 1.0
        assert w["max_iterations_rate"] == 0.0
        reset_trail_aggregator()

    @pytest.mark.asyncio
    async def test_max_iterations_recorded(self) -> None:
        """think 始终返回 retrieve → 触发 max_iterations，轨迹记录兜圈。"""
        reset_trail_aggregator()
        engine = _make_engine(llm_response="retrieve", max_iterations=2)

        async for _ in engine.answer("测试", "user-1", "session-max-iter"):
            pass

        summary = await get_trail_aggregator().window_summary()
        w = summary["window"]
        assert w["total_sessions"] == 1
        assert w["max_iterations_rate"] == 1.0
        assert w["loitering_rate"] == 1.0
        reset_trail_aggregator()


class TestStuckProtection:
    """P1-7 卡死监控与超时分级测试。"""

    @pytest.mark.asyncio
    async def test_think_with_timeout_passes_through(self) -> None:
        """正常 think 结果原样返回。"""
        engine = _make_engine(llm_response="generate")
        state = _make_state()
        decision = await engine._think_with_timeout(state)
        assert decision == "generate"
        assert state.get("_step_timeouts", 0) == 0

    @pytest.mark.asyncio
    async def test_think_with_timeout_returns_none_on_timeout(self) -> None:
        """think 超过单步骤超时 → 返回 None 并计数告警。"""
        engine = _make_engine()
        engine._step_timeout_s = 0.05  # 50ms 超时

        async def _slow_think(state: AgentState) -> str:
            await asyncio.sleep(1)
            return "generate"

        engine._think = _slow_think  # type: ignore[method-assign]
        state = _make_state()
        decision = await engine._think_with_timeout(state)
        assert decision is None
        assert state["_step_timeouts"] == 1

    @pytest.mark.asyncio
    async def test_total_timeout_breaks_loop(self) -> None:
        """总任务超时 → 决策循环立即退出并标记 _total_timeout。"""
        engine = _make_engine(llm_response="retrieve")
        engine._total_timeout_s = 0.0  # 立即超时
        state = _make_state()

        async for _ in engine._run_decision_loop_streaming(state):
            pass

        assert state["_total_timeout"] is True
        assert state["iteration"] == 0  # 未进入任何迭代

    @pytest.mark.asyncio
    async def test_max_iterations_sets_alert_flag(self) -> None:
        """超过最大迭代 → 标记 _max_iter_hit。"""
        engine = _make_engine(llm_response="retrieve", max_iterations=2)
        state = _make_state(max_iterations=2)

        async for _ in engine._run_decision_loop_streaming(state):
            pass

        assert state["_max_iter_hit"] is True
        assert state["iteration"] == 3  # 第 3 轮触发上限退出
