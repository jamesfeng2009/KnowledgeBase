"""P4-E 焦点注入引擎测试 — AgentState + _think 焦点注入。"""

import pytest

from app.rag.engine import AgentState, AgenticRAGEngine


class CapturingLLM:
    """Mock LLM — 捕获消息并返回固定决策。"""

    def __init__(self, decision: str = "generate"):
        self._decision = decision
        self.captured_messages: list = []

    async def chat(self, messages, stream=False, **kwargs):
        self.captured_messages = list(messages)
        yield self._decision


class MockComponent:
    """Minimal mock for engine dependencies."""

    async def search(self, *args, **kwargs):
        return []

    async def rerank(self, *args, **kwargs):
        return []

    async def generate(self, *args, **kwargs):
        return ""

    async def call_tool(self, *args, **kwargs):
        return {}


def _make_engine(decision: str = "generate") -> tuple[AgenticRAGEngine, CapturingLLM]:
    """创建最小化引擎实例用于测试 _think()。"""
    llm = CapturingLLM(decision=decision)
    engine = AgenticRAGEngine(
        llm=llm,
        mcp_client=MockComponent(),
        retriever=MockComponent(),
        reranker=MockComponent(),
        generator=MockComponent(),
    )
    return engine, llm


def _make_base_state(**overrides) -> AgentState:
    """创建基础 AgentState 用于 _think() 测试。"""
    state: AgentState = {
        "query": "测试查询",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "测试查询"},
        ],
        "iteration": 1,
        "max_iterations": 5,
        "retrieved_docs": [],
        "tool_results": [],
        "conversation_focus": None,
        "drift_info": None,
    }
    state.update(overrides)
    return state


class TestAgentStateFocusFields:
    """AgentState 新增字段测试。"""

    def test_state_accepts_conversation_focus(self):
        """AgentState 接受 conversation_focus 字段。"""
        state: AgentState = {
            "query": "test",
            "conversation_focus": {"topic": "限号", "entity": "北京"},
            "drift_info": {"is_drift": True},
        }
        assert state["conversation_focus"]["topic"] == "限号"
        assert state["drift_info"]["is_drift"] is True

    def test_state_focus_optional(self):
        """AgentState 新字段可选 — 不传也能工作。"""
        state: AgentState = {"query": "test"}
        assert state.get("conversation_focus") is None
        assert state.get("drift_info") is None


class TestThinkFocusInjection:
    """_think 焦点注入测试。"""

    @pytest.mark.asyncio
    async def test_focus_injected_into_think(self):
        """有焦点时，_think 的动态上下文包含焦点信息。"""
        engine, llm = _make_engine()
        state = _make_base_state(
            conversation_focus={"topic": "限号政策", "entity": "北京", "intent": "查询"},
        )

        await engine._think(state)

        # 检查最后一条消息（dynamic context）包含焦点
        dynamic_msg = llm.captured_messages[-1]["content"]
        assert "限号政策" in dynamic_msg
        assert "北京" in dynamic_msg
        assert "查询" in dynamic_msg

    @pytest.mark.asyncio
    async def test_no_focus_not_injected(self):
        """无焦点时，_think 的动态上下文不包含焦点信息。"""
        engine, llm = _make_engine()
        state = _make_base_state(conversation_focus=None)

        await engine._think(state)

        dynamic_msg = llm.captured_messages[-1]["content"]
        assert "对话焦点" not in dynamic_msg

    @pytest.mark.asyncio
    async def test_drift_warning_injected(self):
        """有漂移时，_think 的动态上下文包含漂移警告。"""
        engine, llm = _make_engine()
        state = _make_base_state(
            drift_info={"is_drift": True, "drift_score": 0.8},
        )

        await engine._think(state)

        dynamic_msg = llm.captured_messages[-1]["content"]
        assert "切换了话题" in dynamic_msg

    @pytest.mark.asyncio
    async def test_no_drift_no_warning(self):
        """无漂移时，_think 的动态上下文不包含漂移警告。"""
        engine, llm = _make_engine()
        state = _make_base_state(
            drift_info={"is_drift": False, "drift_score": 0.1},
        )

        await engine._think(state)

        dynamic_msg = llm.captured_messages[-1]["content"]
        assert "切换了话题" not in dynamic_msg

    @pytest.mark.asyncio
    async def test_both_focus_and_drift(self):
        """同时有焦点和漂移时，两者都注入。"""
        engine, llm = _make_engine()
        state = _make_base_state(
            conversation_focus={"topic": "报销", "entity": "员工", "intent": "查询"},
            drift_info={"is_drift": True},
        )

        await engine._think(state)

        dynamic_msg = llm.captured_messages[-1]["content"]
        assert "报销" in dynamic_msg
        assert "切换了话题" in dynamic_msg

    @pytest.mark.asyncio
    async def test_drift_info_none_no_warning(self):
        """drift_info 为 None 时，不注入漂移警告。"""
        engine, llm = _make_engine()
        state = _make_base_state(drift_info=None)

        await engine._think(state)

        dynamic_msg = llm.captured_messages[-1]["content"]
        assert "切换了话题" not in dynamic_msg

    @pytest.mark.asyncio
    async def test_focus_with_empty_values(self):
        """焦点字段值为空字符串时也能正常注入。"""
        engine, llm = _make_engine()
        state = _make_base_state(
            conversation_focus={"topic": "", "entity": "", "intent": ""},
        )

        await engine._think(state)

        dynamic_msg = llm.captured_messages[-1]["content"]
        assert "对话焦点" in dynamic_msg
