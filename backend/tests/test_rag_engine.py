"""RAG 引擎测试 — AgenticRAGEngine 的决策循环、检索与反思。

使用 Mock 实现 LLM / 检索器 / 重排器 / 生成器，隔离外部依赖，
聚焦验证引擎的流程编排逻辑（think → retrieve/tool_call → generate → reflect）。

核心验证点：
- AgentState 初始化字段完整；
- _parse_decision 正确解析三种路由信号；
- _think 委托 LLM 并返回决策；
- 权限过滤在重排之前执行（核心安全约束）；
- _reflect 正常执行不抛异常。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.rag.engine import (
    _CLARIFY_ANSWER,
    _INTERRUPT_ANSWER,
    AgentState,
    AgenticRAGEngine,
)
from app.utils.sse import SSEEvent, SSEEventType


# ======================================================================
# Mock 实现
# ======================================================================


class FakeLLM:
    """Mock LLM Provider — 按预设文本响应 chat 调用。"""

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


class FakeRetriever:
    """Mock HybridRetriever — 返回预设候选文档，记录接收到的查询。"""

    def __init__(self, candidates: list[dict[str, Any]] | None = None) -> None:
        self.candidates = candidates or []
        self.search_query: str = ""

    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.search_query = query
        return self.candidates


class FakeReranker:
    """Mock RerankerBase — 记录调用与接收到的查询和文档。"""

    def __init__(self) -> None:
        self.called: bool = False
        self.received_docs: list[dict[str, Any]] = []
        self.rerank_query: str = ""

    async def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        self.called = True
        self.rerank_query = query
        self.received_docs = list(documents)
        return [
            {"index": i, "score": 0.9 - i * 0.1, "content": d.get("content", "")}
            for i, d in enumerate(documents)
        ]


class FakeGenerator:
    """Mock Generator — 逐 token yield 预设文本。"""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["这是", "答案"]

    async def generate(
        self,
        query: str,
        retrieved_docs: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        memory_context: str = "",
        constraint_context: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        for token in self.tokens:
            yield token


class FakeMCPClient:
    """Mock MCPClient — 无工具可用。"""

    async def get_tools_for_llm(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        return "{}"


class FakeQueryRewriter:
    """Mock QueryRewriter — 返回原查询，避免测试触发真实 LLM 重写调用。"""

    async def rewrite(self, query: str, context: str = "", **kwargs: Any) -> Any:
        from app.rag.query_rewriter import QueryRewriteResult

        return QueryRewriteResult(original=query, strategy=[])


class SequenceLLM:
    """Mock LLM Provider — 按预设序列逐次响应 chat 调用（超出后复用最后一个）。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.i = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        resp = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        yield resp


# ======================================================================
# 测试
# ======================================================================


# 哨兵 — 区分「默认注入 FakeQueryRewriter」与「显式传 None 走 engine 自动初始化」
_NO_REWRITER = object()


def _make_engine(
    llm_response: str = "generate",
    candidates: list[dict[str, Any]] | None = None,
    permission_filter=None,
    max_iterations: int = 5,
    query_rewriter: Any = _NO_REWRITER,
) -> tuple[AgenticRAGEngine, FakeLLM, FakeRetriever, FakeReranker]:
    """构造带 Mock 组件的 RAG 引擎。"""
    llm = FakeLLM(llm_response)
    retriever = FakeRetriever(candidates)
    reranker = FakeReranker()
    generator = FakeGenerator()
    engine = AgenticRAGEngine(
        llm=llm,
        mcp_client=FakeMCPClient(),
        retriever=retriever,
        reranker=reranker,
        generator=generator,
        cache=None,
        permission_filter=permission_filter,
        max_iterations=max_iterations,
        # 默认注入假重写器，避免测试触发真实 LLM 重写调用；
        # 显式传 None 时透传给 engine（走自动初始化路径）
        query_rewriter=(
            FakeQueryRewriter() if query_rewriter is _NO_REWRITER else query_rewriter
        ),
    )
    return engine, llm, retriever, reranker


def _make_state(**overrides: Any) -> AgentState:
    """构造测试用 AgentState。"""
    state: AgentState = {
        "query": "报销流程怎么走",
        "rewritten_query": None,
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
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


# ----------------------------------------------------------------------


class TestAgentStateInitialization:
    """AgentState 初始化测试。"""

    def test_agent_state_initialization(self) -> None:
        """AgentState 应包含所有必需字段且初始值正确。"""
        state = _make_state()

        assert state["query"] == "报销流程怎么走"
        assert state["user_id"] == "user-001"
        assert state["session_id"] == "session-001"
        assert state["messages"] == []
        assert state["retrieved_docs"] == []
        assert state["tool_results"] == []
        assert state["answer"] == ""
        assert state["iteration"] == 0
        assert state["max_iterations"] == 5

    def test_engine_stores_components(self) -> None:
        """引擎应正确存储注入的组件。"""
        engine, llm, retriever, reranker = _make_engine()

        assert engine.llm is llm
        assert engine.retriever is retriever
        assert engine.reranker is reranker
        assert engine.max_iterations == 5


class TestParseDecision:
    """_parse_decision 路由信号解析测试。"""

    def test_parse_decision_retrieve(self) -> None:
        """包含 'retrieve' 的文本应解析为 retrieve。"""
        assert AgenticRAGEngine._parse_decision("please retrieve knowledge") == "retrieve"

    def test_parse_decision_tool_call(self) -> None:
        """包含 'tool' 的文本应解析为 tool_call。"""
        assert AgenticRAGEngine._parse_decision("use tool to query") == "tool_call"

    def test_parse_decision_generate(self) -> None:
        """不匹配 retrieve/tool 的文本应解析为 generate。"""
        assert AgenticRAGEngine._parse_decision("generate the answer") == "generate"
        assert AgenticRAGEngine._parse_decision("random text") == "generate"


class TestThink:
    """_think 决策测试。"""

    @pytest.mark.asyncio
    async def test_think_returns_decision(self) -> None:
        """_think 应委托 LLM 并返回解析后的决策信号。"""
        engine, _, _, _ = _make_engine(llm_response="retrieve")
        state = _make_state(iteration=1)

        decision = await engine._think(state)

        assert decision == "retrieve"

    @pytest.mark.asyncio
    async def test_think_fallback_on_error(self) -> None:
        """LLM 异常时 _think 应降级返回 generate。"""

        class ErrorLLM:
            async def chat(self, *args, **kwargs):
                raise RuntimeError("LLM unavailable")
                yield  # noqa: E701 — 使其为 async generator

        engine = AgenticRAGEngine(
            llm=ErrorLLM(),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
        )
        state = _make_state(iteration=1)

        decision = await engine._think(state)

        assert decision == "generate"


class TestRetrieve:
    """_retrieve 检索流程测试 — 重点验证权限过滤在重排之前。"""

    @pytest.mark.asyncio
    async def test_retrieve_permission_filter_before_rerank(self) -> None:
        """权限过滤必须在重排之前执行。

        验证：
        1. permission_filter 先于 reranker.rerank 被调用；
        2. reranker 接收到的是过滤后的文档（不含越权文档）。
        """
        candidates = [
            {"chunk_id": "1", "content": "公开文档", "classification": "public"},
            {"chunk_id": "2", "content": "机密文档", "classification": "secret"},
            {"chunk_id": "3", "content": "内部文档", "classification": "internal"},
        ]

        call_order: list[str] = []

        async def permission_filter(docs: list[dict]) -> list[dict]:
            call_order.append("permission_filter")
            # 过滤掉 secret 文档
            return [d for d in docs if d.get("classification") != "secret"]

        class OrderTrackingReranker:
            def __init__(self) -> None:
                self.received_docs: list[dict] = []

            async def rerank(self, query, documents, top_k=5):
                call_order.append("rerank")
                self.received_docs = list(documents)
                return [
                    {"index": i, "score": 0.9, "content": d.get("content", "")}
                    for i, d in enumerate(documents)
                ]

        reranker = OrderTrackingReranker()
        engine = AgenticRAGEngine(
            llm=FakeLLM(),
            mcp_client=FakeMCPClient(),
            retriever=FakeRetriever(candidates),
            reranker=reranker,
            generator=FakeGenerator(),
            permission_filter=permission_filter,
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=None)

        # 权限过滤在重排之前
        assert call_order[0] == "permission_filter"
        assert call_order[1] == "rerank"
        # 重排器收到的是过滤后的文档（不含 secret）
        assert len(reranker.received_docs) == 2
        assert all(
            d.get("classification") != "secret" for d in reranker.received_docs
        )

    @pytest.mark.asyncio
    async def test_retrieve_without_permission_filter(self) -> None:
        """未注入 permission_filter 时，所有候选直接进入重排。"""
        candidates = [
            {"chunk_id": "1", "content": "文档 A"},
            {"chunk_id": "2", "content": "文档 B"},
        ]
        engine, _, _, reranker = _make_engine(
            candidates=candidates, permission_filter=None
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=None)

        assert reranker.called
        assert len(reranker.received_docs) == 2

    @pytest.mark.asyncio
    async def test_retrieve_permission_error_returns_empty(self) -> None:
        """权限过滤异常时应保守返回空列表，避免泄露越权文档。"""

        async def error_filter(docs: list[dict]) -> list[dict]:
            raise RuntimeError("permission service down")

        engine, _, _, reranker = _make_engine(
            candidates=[{"chunk_id": "1", "content": "doc"}],
            permission_filter=error_filter,
        )
        state = _make_state()

        await engine._retrieve(state, kb_ids=None)

        # 权限过滤出错 -> 重排器未被调用 -> retrieved_docs 为空
        assert not reranker.called
        assert state["retrieved_docs"] == []


class TestReflect:
    """_reflect 反思测试。"""

    @pytest.mark.asyncio
    async def test_reflect_quality_check(self) -> None:
        """_reflect 应正常执行并记录反思结论，不抛异常。"""
        engine, _, _, _ = _make_engine(llm_response="satisfied")
        state = _make_state(answer="这是一个完整的回答，包含引用来源。")

        # 不应抛出异常
        await engine._reflect(state)

        # answer 未被修改
        assert "完整的回答" in state["answer"]


class TestAnswerStream:
    """answer 主入口流式输出测试。"""

    @pytest.mark.asyncio
    async def test_answer_yields_tokens(self) -> None:
        """answer 应流式 yield 生成器的 token。"""
        engine, _, _, _ = _make_engine(llm_response="generate")

        tokens = []
        async for chunk in engine.answer("test query", "user-1", "session-1"):
            if isinstance(chunk, str):
                tokens.append(chunk)

        assert "".join(tokens) == "这是答案"


class TestRuleLevelExits:
    """P0: 规则级出口（Clarify / Interrupt）测试。"""

    @pytest.mark.asyncio
    async def test_clarify_exit_on_empty_retrieval(self) -> None:
        """连续两次空检索 → Clarify 出口：产出澄清文案，不进入 LLM 生成。"""
        engine, _, _, _ = _make_engine(llm_response="retrieve", candidates=[])

        events: list[SSEEvent] = []
        tokens: list[str] = []
        async for chunk in engine.answer("test query", "user-1", "session-1"):
            if isinstance(chunk, SSEEvent):
                events.append(chunk)
            elif isinstance(chunk, str):
                tokens.append(chunk)

        # 应产出 clarify 事件
        assert any(e.event == SSEEventType.CLARIFY for e in events)
        # 答案应为固定澄清文案（未调用 LLM 生成）
        assert "".join(tokens) == _CLARIFY_ANSWER
        # 不应产出普通生成结果
        assert "这是答案" not in "".join(tokens)

    @pytest.mark.asyncio
    async def test_interrupt_exit_on_repeated_decision(self) -> None:
        """同一决策连续重复 ≥ 3 → Interrupt 出口：产出中断文案。"""
        engine, _, _, _ = _make_engine(
            llm_response="retrieve", candidates=[{"content": "文档内容"}]
        )

        events: list[SSEEvent] = []
        tokens: list[str] = []
        async for chunk in engine.answer("test query", "user-1", "session-1"):
            if isinstance(chunk, SSEEvent):
                events.append(chunk)
            elif isinstance(chunk, str):
                tokens.append(chunk)

        # 应产出 interrupt 事件
        assert any(e.event == SSEEventType.INTERRUPT for e in events)
        # 答案应为固定中断文案
        assert "".join(tokens) == _INTERRUPT_ANSWER

    @pytest.mark.asyncio
    async def test_single_empty_retrieval_does_not_clarify(self) -> None:
        """仅一次空检索不应触发 Clarify — 允许 Agent 换查询/换库再试。"""
        # 第一次 retrieve 返回空，第二次返回文档，随后 generate → 正常继续
        class _TwoPhaseRetriever:
            def __init__(self) -> None:
                self.calls = 0

            async def search(self, query, kb_ids=None, top_k=20, filters=None):
                self.calls += 1
                if self.calls == 1:
                    return []
                return [{"content": "文档内容"}]

        llm = SequenceLLM(["retrieve", "retrieve", "generate"])
        retriever = _TwoPhaseRetriever()
        reranker = FakeReranker()
        generator = FakeGenerator()
        engine = AgenticRAGEngine(
            llm=llm,
            mcp_client=FakeMCPClient(),
            retriever=retriever,  # type: ignore[arg-type]
            reranker=reranker,
            generator=generator,
            cache=None,
            max_iterations=5,
            query_rewriter=FakeQueryRewriter(),
        )

        events: list[SSEEvent] = []
        tokens: list[str] = []
        async for chunk in engine.answer("test query", "user-1", "session-1"):
            if isinstance(chunk, SSEEvent):
                events.append(chunk)
            elif isinstance(chunk, str):
                tokens.append(chunk)

        # 不应触发 Clarify / Interrupt
        assert not any(e.event == SSEEventType.CLARIFY for e in events)
        assert not any(e.event == SSEEventType.INTERRUPT for e in events)
        # 正常生成
        assert "".join(tokens) == "这是答案"


class TestSuccessCriteria:
    """P1: 成功标准显式化（Goal State）测试。"""

    @pytest.mark.asyncio
    async def test_criteria_satisfied_when_met(self) -> None:
        """答案满足全部成功标准 → criteria_satisfied=True。"""
        llm = SequenceLLM([
            '{"satisfied": true, "unmet": [], "reason": "完整覆盖"}'
        ])
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(
            answer="报销流程是提交申请后 3 个工作日内审批。",
            success_criteria=["答案应明确报销流程", "答案应基于政策文档"],
        )

        await engine._check_success_criteria(state)

        assert state["criteria_satisfied"] is True
        assert state["unmet_criteria"] == []

    @pytest.mark.asyncio
    async def test_criteria_not_satisfied_when_unmet(self) -> None:
        """答案未满足部分成功标准 → criteria_satisfied=False，记录 unmet。"""
        llm = SequenceLLM([
            '{"satisfied": false, "unmet": ["答案应明确报销流程"],'
            ' "reason": "缺少流程细节"}'
        ])
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(
            answer="报销需要提交申请。",
            success_criteria=["答案应明确报销流程", "答案应基于政策文档"],
        )

        await engine._check_success_criteria(state)

        assert state["criteria_satisfied"] is False
        assert state["unmet_criteria"] == ["答案应明确报销流程"]

    @pytest.mark.asyncio
    async def test_no_criteria_skips_llm(self) -> None:
        """无成功标准 → 视为满足且不调用 LLM。"""
        llm = SequenceLLM(["unused"])
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(answer="答案")

        await engine._check_success_criteria(state)

        assert state["criteria_satisfied"] is True
        assert llm.i == 0  # 未调用 LLM

    @pytest.mark.asyncio
    async def test_invalid_json_conservative_satisfied(self) -> None:
        """LLM 返回非法 JSON → 保守视为满足（不阻断正常答案）。"""
        llm = SequenceLLM(["无法解析"])
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(
            answer="答案",
            success_criteria=["标准1"],
        )

        await engine._check_success_criteria(state)

        assert state["criteria_satisfied"] is True

    @pytest.mark.asyncio
    async def test_reflect_sets_criteria_satisfied(self) -> None:
        """_reflect 对照成功标准判定 Finish — 结果写入 state 与 _span_evidence。"""
        # 第一个响应给 _reflect_inline，第二个给 _check_success_criteria
        llm = SequenceLLM([
            "satisfied",
            '{"satisfied": true, "unmet": [], "reason": "ok"}',
        ])
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(
            answer="报销流程是提交申请后 3 个工作日内审批。",
            retrieved_docs=[{"content": "报销政策文档内容"}],
            success_criteria=["答案应明确报销流程"],
        )

        await engine._reflect(state)

        assert state["criteria_satisfied"] is True
        assert state["_span_evidence"]["criteria_satisfied"] is True


class RecordingLLM:
    """Mock LLM — 记录最近一次 chat 的 messages，返回预设响应。"""

    def __init__(self, response: str = "generate") -> None:
        self.response = response
        self.last_messages: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str | dict]:
        self.last_messages = list(messages)
        yield self.response


class TestObservations:
    """P2: 结构化观察记录 — 写入 / think 动态上下文复用 / span 复用。"""

    @pytest.mark.asyncio
    async def test_record_observation_writes_structured_entry(self) -> None:
        """_record_observation 写入带 iteration/kind/detail 的结构化条目。"""
        engine, _, _, _ = _make_engine()
        state = _make_state(iteration=2)

        engine._record_observation(state, "success", "重排后保留 3 篇文档")

        assert state["observations"] == [
            {"iteration": 2, "kind": "success", "detail": "重排后保留 3 篇文档"}
        ]

    @pytest.mark.asyncio
    async def test_record_observation_accumulates(self) -> None:
        """多次写入按序累积，且 iteration 缺省取当前轮次。"""
        engine, _, _, _ = _make_engine()
        state = _make_state(iteration=1)

        engine._record_observation(state, "progress", "检索到 5 篇候选文档")
        state["iteration"] = 2
        engine._record_observation(state, "success", "重排后保留 3 篇文档")

        assert [o["kind"] for o in state["observations"]] == ["progress", "success"]
        assert state["observations"][0]["iteration"] == 1
        assert state["observations"][1]["iteration"] == 2

    @pytest.mark.asyncio
    async def test_retrieve_records_observations(self) -> None:
        """_retrieve 写入候选召回 progress 与重排成功 success 观察。"""
        engine, _, _, _ = _make_engine(
            candidates=[{"content": "文档1"}, {"content": "文档2"}]
        )
        state = _make_state(iteration=1)

        await engine._retrieve(state, kb_ids=None)

        kinds = [o["kind"] for o in state["observations"]]
        assert "progress" in kinds  # 候选召回
        assert "success" in kinds  # 重排后保留
        assert all(o["iteration"] == 1 for o in state["observations"])

    @pytest.mark.asyncio
    async def test_retrieve_empty_records_anomaly(self) -> None:
        """检索为空 → 写入 anomaly 观察。"""
        engine, _, _, _ = _make_engine(candidates=[])
        state = _make_state(iteration=1)

        await engine._retrieve(state, kb_ids=None)

        assert any(
            o["kind"] == "anomaly" and "检索为空" in o["detail"]
            for o in state["observations"]
        )

    @pytest.mark.asyncio
    async def test_think_dynamic_context_includes_observations(self) -> None:
        """_think 动态上下文注入最近观察，且只取尾部 N 条。"""
        llm = RecordingLLM("retrieve")
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(iteration=1)
        state["observations"] = [
            {"iteration": 1, "kind": "progress", "detail": f"观察{i}"}
            for i in range(10)
        ]

        await engine._think(state)

        joined = "".join(
            m.get("content", "") for m in llm.last_messages if m.get("role") == "user"
        )
        assert "最近观察" in joined
        # 只注入尾部 _OBSERVATIONS_IN_THINK 条
        assert "观察9" in joined
        assert "观察0" not in joined

    @pytest.mark.asyncio
    async def test_think_skips_observations_when_empty(self) -> None:
        """无观察记录时动态上下文不出现"最近观察"段。"""
        llm = RecordingLLM("retrieve")
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(iteration=1)

        await engine._think(state)

        joined = "".join(
            m.get("content", "") for m in llm.last_messages if m.get("role") == "user"
        )
        assert "最近观察" not in joined

    @pytest.mark.asyncio
    async def test_reflect_span_evidence_carries_observations(self) -> None:
        """_reflect 的 _span_evidence 携带完整观察列表供离线评测回溯。"""
        llm = SequenceLLM([
            "satisfied",
            '{"satisfied": true, "unmet": [], "reason": "ok"}',
        ])
        engine, _, _, _ = _make_engine(llm_response="generate")
        engine.llm = llm
        state = _make_state(
            answer="报销流程是提交申请后 3 个工作日内审批。",
            retrieved_docs=[{"content": "报销政策文档内容"}],
            observations=[
                {"iteration": 1, "kind": "success", "detail": "重排后保留 1 篇文档"}
            ],
        )

        await engine._reflect(state)

        assert state["_span_evidence"]["observations"] == [
            {"iteration": 1, "kind": "success", "detail": "重排后保留 1 篇文档"}
        ]

    @pytest.mark.asyncio
    async def test_tool_call_records_success_observation(self) -> None:
        """工具执行成功 → 写入 success 观察。"""

        class _ToolMCPClient:
            async def get_tools_for_llm(self) -> list[dict[str, Any]]:
                return []

            async def call_tool(
                self, tool_name: str, arguments: dict, tenant_id: str | None = None
            ) -> str:
                return '{"ok": true}'

        from app.rag.engine import ToolUse

        engine = AgenticRAGEngine(
            llm=FakeLLM("generate"),
            mcp_client=_ToolMCPClient(),  # type: ignore[arg-type]
            retriever=FakeRetriever(),
            reranker=FakeReranker(),
            generator=FakeGenerator(),
            cache=None,
            max_iterations=5,
            query_rewriter=FakeQueryRewriter(),
        )
        state = _make_state(iteration=1)
        tool_use: ToolUse = {"name": "knowledge_search", "input": {}, "id": "t1"}

        async for _ in engine._execute_tool_use(state, tool_use):
            pass

        assert any(
            o["kind"] == "success" and "knowledge_search" in o["detail"]
            for o in state["observations"]
        )
