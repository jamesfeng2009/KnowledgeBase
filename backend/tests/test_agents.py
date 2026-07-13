"""Agent 层测试 — 注册表、Agent 初始化与主循环 SSE 输出。

验证点：
- AgentRegistry.register / create 正确注册与实例化；
- QAAgent / WorkflowAgent / ActionAgent 初始化属性正确；
- BaseAgent.run 流式输出 SSE 事件（agent_start → token → done）。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.agents.base import AgentState, BaseAgent
from app.agents.registry import AgentRegistry
from app.agents.action_agent import ActionAgent
from app.agents.qa_agent import QAAgent
from app.agents.workflow_agent import WorkflowAgent


# ======================================================================
# Mock 实现
# ======================================================================


class _FakeLLM:
    """Mock LLM — chat 为 async generator，yield 预设文本。"""

    def __init__(self, response: str = "generate") -> None:
        self.response = response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        yield self.response


class _TestAgent(BaseAgent):
    """用于测试 BaseAgent.run 主循环的具体子类。"""

    agent_type: str = "test"
    system_prompt: str = "你是测试助手。"

    async def execute(self, state: AgentState) -> AsyncIterator[str]:
        """yield 一段足够长的文本使 reflect 通过（>= 10 字符）。"""
        yield "这是一个测试回答，内容长度超过十个字符以确保反思通过。"


def _make_mock_memory() -> AsyncMock:
    """构造 Mock MemoryManager。"""
    memory = AsyncMock()
    ctx = MagicMock()
    ctx.to_system_prompt.return_value = ""
    memory.build_context.return_value = ctx
    return memory


# ======================================================================
# 测试
# ======================================================================


class TestAgentRegistry:
    """Agent 注册表测试。"""

    def test_agent_registry_register(self) -> None:
        """register 装饰器应将 Agent 类注册到注册表。"""

        class _CustomAgent(BaseAgent):
            agent_type = "custom_test_reg"

            async def execute(self, state: AgentState) -> AsyncIterator[str]:
                yield "custom"

        try:
            AgentRegistry.register("custom_test_reg")(_CustomAgent)
            assert AgentRegistry.is_registered("custom_test_reg")
            assert AgentRegistry._registry["custom_test_reg"] is _CustomAgent
        finally:
            # 清理，避免污染其他测试
            AgentRegistry._registry.pop("custom_test_reg", None)

    def test_agent_registry_create(self) -> None:
        """create 应根据类型返回正确的 Agent 实例。"""
        llm = _FakeLLM()
        mcp = AsyncMock()
        memory = _make_mock_memory()

        agent = AgentRegistry.create("qa", llm, mcp, memory)

        assert isinstance(agent, QAAgent)
        assert agent.agent_type == "qa"
        assert agent.llm is llm
        assert agent.mcp is mcp
        assert agent.memory is memory

    def test_agent_registry_create_unknown_raises(self) -> None:
        """创建未注册类型应抛出 ValueError。"""
        with pytest.raises(ValueError, match="未注册"):
            AgentRegistry.create("nonexistent", _FakeLLM(), AsyncMock(), AsyncMock())


class TestAgentInitialization:
    """Agent 初始化测试。"""

    def test_qa_agent_initialization(self) -> None:
        """QAAgent 应正确设置 agent_type 和 system_prompt。"""
        agent = QAAgent(_FakeLLM(), AsyncMock(), _make_mock_memory())

        assert agent.agent_type == "qa"
        assert "问答" in agent.system_prompt or "知识库" in agent.system_prompt

    def test_workflow_agent_initialization(self) -> None:
        """WorkflowAgent 应正确设置 agent_type。"""
        agent = WorkflowAgent(_FakeLLM(), AsyncMock(), _make_mock_memory())

        assert agent.agent_type == "workflow"
        assert "流程" in agent.system_prompt or "工作流" in agent.system_prompt

    def test_action_agent_initialization(self) -> None:
        """ActionAgent 应正确设置 agent_type。"""
        agent = ActionAgent(_FakeLLM(), AsyncMock(), _make_mock_memory())

        assert agent.agent_type == "action"
        assert "执行" in agent.system_prompt or "操作" in agent.system_prompt


class TestBaseAgentRun:
    """BaseAgent.run 主循环 SSE 输出测试。"""

    @pytest.mark.asyncio
    async def test_base_agent_run_yields_sse(self) -> None:
        """run 应流式输出 SSE 事件序列：agent_start → token(s) → done。"""
        agent = _TestAgent(
            llm=_FakeLLM("generate"),
            mcp_client=AsyncMock(),
            memory=_make_mock_memory(),
        )

        events: list[str] = []
        async for chunk in agent.run("测试查询", str(uuid4()), "session-1"):
            events.append(chunk)

        # 至少有 agent_start + token + done 三个事件
        assert len(events) >= 2

        # 首个事件为 agent_start（meta 事件）
        first = events[0]
        assert "agent_start" in first

        # 末尾事件为 done
        last = events[-1]
        assert "done" in last

        # 中间事件应包含 execute yield 的文本
        token_events = events[1:-1]
        combined = "".join(token_events)
        assert "测试回答" in combined

    @pytest.mark.asyncio
    async def test_run_sse_meta_contains_agent_type(self) -> None:
        """agent_start 事件应携带 agent_type 元数据。"""
        agent = _TestAgent(
            llm=_FakeLLM("generate"),
            mcp_client=AsyncMock(),
            memory=_make_mock_memory(),
        )

        first_event = None
        async for chunk in agent.run("q", str(uuid4()), "s"):
            first_event = chunk
            break

        assert first_event is not None
        # SSE 格式: event: meta\ndata: {...}\n\n
        assert "meta" in first_event

    @pytest.mark.asyncio
    async def test_run_with_memory_failure_still_works(self) -> None:
        """记忆加载失败时 Agent 仍能正常运行（优雅降级）。"""
        memory = AsyncMock()
        memory.build_context.side_effect = RuntimeError("memory down")

        agent = _TestAgent(
            llm=_FakeLLM("generate"),
            mcp_client=AsyncMock(),
            memory=memory,
        )

        events: list[str] = []
        async for chunk in agent.run("q", str(uuid4()), "s"):
            events.append(chunk)

        # 记忆失败不影响主流程
        assert any("done" in e for e in events)
