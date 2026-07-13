"""
Agent 基类 — 单一职责：定义 Agent Loop 主循环与通用状态管理。

Agent Loop 遵循 think → action → reflect 循环范式：
  think    — LLM 决策下一步行动（检索 / 工具调用 / 直接生成）
  action   — 执行具体操作（子类实现 execute）
  reflect  — 自我反思，判断结果是否充分、是否需要重试

遵循开闭原则：BaseAgent 定义主循环骨架，子类只需实现 execute 方法，
无需修改 think / reflect / run 的循环逻辑。
遵循依赖倒置：Agent 依赖 LLMProvider / MCPClient / MemoryManager 抽象，
不感知底层是 Anthropic API 还是本地 vLLM。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, TypedDict

from app.llm.base import LLMProvider, Message
from app.mcp.client import MCPClient
from app.memory.memory_manager import MemoryManager
from app.utils.logger import get_logger
from app.utils.sse import format_sse_event

logger = get_logger(__name__)


class AgentState(TypedDict, total=False):
    """Agent 运行时状态 — 在 think / action / reflect 之间传递。

    使用 total=False 允许字段可选，初始状态只需提供 query / user_id / session_id。

    Attributes:
        query: 用户原始查询。
        user_id: 用户 ID。
        session_id: 会话 ID。
        messages: 发送给 LLM 的消息列表（含历史上下文）。
        retrieved_docs: 检索到的文档列表。
        tool_results: 工具调用结果列表。
        answer: 当前生成的答案。
        iteration: 当前迭代轮次。
    """

    query: str
    user_id: str
    session_id: str
    messages: list[Message]
    retrieved_docs: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    answer: str
    iteration: int


class BaseAgent(ABC):
    """Agent 抽象基类 — 定义 Agent Loop 主循环。

    子类需实现：
    - system_prompt: Agent 专属系统提示词
    - agent_type: Agent 类型标识
    - execute(): 具体执行逻辑（检索 / 工具调用 / 生成）

    主循环（run 方法）：
    1. 初始化 AgentState
    2. 循环最多 max_iterations 次：
       a. think — LLM 决策下一步
       b. execute — 子类实现具体操作，yield SSE token
       c. reflect — 自我反思，判断是否需要重试
    3. 保存记忆上下文
    """

    #: Agent 类型标识，子类覆盖（如 "qa" / "workflow" / "action"）。
    agent_type: str = "base"

    #: Agent 系统提示词，子类覆盖。
    system_prompt: str = "你是一个企业知识库 AI 助手。"

    #: 最大迭代次数，防止无限循环。
    max_iterations: int = 5

    def __init__(
        self,
        llm: LLMProvider,
        mcp_client: MCPClient,
        memory: MemoryManager,
    ) -> None:
        """初始化 Agent，注入外部依赖。

        Args:
            llm: LLM Provider 实例，负责文本生成与工具调用。
            mcp_client: MCP 客户端，负责调用外部工具。
            memory: 记忆管理器，负责四级记忆的读写。
        """
        self.llm: LLMProvider = llm
        self.mcp: MCPClient = mcp_client
        self.memory: MemoryManager = memory

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(
        self,
        query: str,
        user_id: str,
        session_id: str,
    ) -> AsyncIterator[str]:
        """Agent Loop 主循环 — think → execute → reflect。

        每次迭代：
        1. think: LLM 决策下一步行动；
        2. execute: 子类实现具体操作，逐 token yield SSE；
        3. reflect: 自我反思，返回是否需要重试。

        最多迭代 max_iterations 次，反思通过或达到上限时结束。

        Args:
            query: 用户输入的查询。
            user_id: 用户 ID。
            session_id: 会话 ID。

        Yields:
            SSE 格式的文本块（token / 元数据 / 结束事件）。
        """
        # 推送 Agent 元数据
        yield format_sse_event(
            json.dumps(
                {"type": "agent_start", "agent_type": self.agent_type},
                ensure_ascii=False,
            ),
            event="meta",
        )

        # 初始化状态
        state: AgentState = AgentState(
            query=query,
            user_id=user_id,
            session_id=session_id,
            messages=[],
            retrieved_docs=[],
            tool_results=[],
            answer="",
            iteration=0,
        )

        # 加载记忆上下文并注入系统提示词
        memory_fragment = await self._load_memory(user_id, session_id, query)
        system_prompt = self.system_prompt
        if memory_fragment:
            system_prompt = system_prompt + "\n\n" + memory_fragment

        state["messages"].append(
            Message(role="system", content=system_prompt)
        )
        state["messages"].append(
            Message(role="user", content=query)
        )

        # Agent Loop 主循环
        for iteration in range(1, self.max_iterations + 1):
            state["iteration"] = iteration
            logger.info(
                "agent.loop_iteration",
                agent_type=self.agent_type,
                iteration=iteration,
                session_id=session_id,
            )

            # think — LLM 决策下一步
            try:
                await self.think(state)
            except Exception as exc:
                logger.error("agent.think_failed", error=str(exc), iteration=iteration)
                yield format_sse_event(
                    json.dumps(
                        {"type": "error", "message": f"思考失败: {exc}"},
                        ensure_ascii=False,
                    ),
                    event="error",
                )
                break

            # execute — 子类实现具体操作，yield token
            answer_parts: list[str] = []
            try:
                async for chunk in self.execute(state):
                    if isinstance(chunk, str):
                        answer_parts.append(chunk)
                        yield format_sse_event(chunk)
            except Exception as exc:
                logger.error("agent.execute_failed", error=str(exc), iteration=iteration)
                yield format_sse_event(
                    json.dumps(
                        {"type": "error", "message": f"执行失败: {exc}"},
                        ensure_ascii=False,
                    ),
                    event="error",
                )
                break

            state["answer"] = "".join(answer_parts)

            # reflect — 自我反思，判断是否需要重试
            try:
                need_retry = await self.reflect(state)
            except Exception as exc:
                logger.error("agent.reflect_failed", error=str(exc), iteration=iteration)
                need_retry = False

            if not need_retry:
                logger.info(
                    "agent.loop_complete",
                    agent_type=self.agent_type,
                    iteration=iteration,
                )
                break

        # 保存记忆上下文
        await self._save_memory(user_id, session_id, state)

        # 推送结束事件
        yield format_sse_event(
            json.dumps(
                {
                    "type": "done",
                    "iteration": state.get("iteration", 0),
                    "agent_type": self.agent_type,
                },
                ensure_ascii=False,
            ),
            event="done",
        )

    # ------------------------------------------------------------------
    # 循环各阶段 — think / reflect 可被子类覆盖
    # ------------------------------------------------------------------

    async def think(self, state: AgentState) -> str:
        """LLM 决策下一步行动。

        默认实现：使用当前消息列表调用 LLM（非流式），获取决策指令。
        子类可覆盖此方法实现更复杂的决策逻辑（如基于检索结果判断是否需要工具调用）。

        Args:
            state: 当前 Agent 状态。

        Returns:
            LLM 输出的决策指令文本（如 "retrieve" / "tool_call" / "generate"）。
        """
        try:
            decision = ""
            async for chunk in self.llm.chat(
                state["messages"],
                stream=False,
                max_tokens=100,
            ):
                if isinstance(chunk, str):
                    decision += chunk
            logger.debug(
                "agent.think_decision",
                decision=decision[:100],
                iteration=state.get("iteration", 0),
            )
            return decision
        except Exception as exc:
            logger.warning("agent.think_fallback", error=str(exc))
            return "generate"

    async def reflect(self, state: AgentState) -> bool:
        """自我反思，返回是否需要重试。

        默认实现：若答案为空或过短（<10 字符），判定为需要重试。
        子类可覆盖此方法实现更细致的反思逻辑（如基于引用准确率、覆盖度）。

        Args:
            state: 当前 Agent 状态（含 answer 字段）。

        Returns:
            True 表示需要重试，False 表示已满足要求。
        """
        answer = state.get("answer", "")
        if not answer or len(answer) < 10:
            logger.info(
                "agent.reflect_retry",
                reason="answer_too_short",
                length=len(answer),
            )
            return True
        return False

    # ------------------------------------------------------------------
    # 抽象方法 — 子类必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute(self, state: AgentState) -> AsyncIterator[str]:
        """执行具体操作 — 子类实现。

        根据当前状态执行检索 / 工具调用 / 文本生成等操作，
        逐 token yield SSE 格式的文本块。

        Args:
            state: 当前 Agent 状态。

        Yields:
            SSE 格式的文本块。
        """
        ...

    # ------------------------------------------------------------------
    # 记忆辅助
    # ------------------------------------------------------------------

    async def _load_memory(
        self,
        user_id: str,
        session_id: str,
        query: str,
    ) -> str:
        """加载记忆上下文并转换为系统提示词片段。

        外部服务不可用时优雅降级，返回空字符串。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。
            query: 当前查询。

        Returns:
            记忆上下文片段（注入系统提示词），失败时返回空字符串。
        """
        try:
            from uuid import UUID

            ctx = await self.memory.build_context(
                user_id=UUID(user_id),
                session_id=session_id,
                recent_messages=[{"role": "user", "content": query}],
            )
            return ctx.to_system_prompt()
        except Exception as exc:
            logger.warning("agent.memory_load_failed", error=str(exc))
            return ""

    async def _save_memory(
        self,
        user_id: str,
        session_id: str,
        state: AgentState,
    ) -> None:
        """保存 Agent 状态到记忆系统。

        外部服务不可用时优雅降级，仅记录日志。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。
            state: Agent 最终状态。
        """
        try:
            from uuid import UUID

            summary = f"用户提问：{state.get('query', '')[:60]}；AI回复：{state.get('answer', '')[:60]}"
            await self.memory.save_session(
                user_id=UUID(user_id),
                session_id=session_id,
                agent_state={
                    "iteration": state.get("iteration", 0),
                    "retrieved_docs": state.get("retrieved_docs", []),
                    "tool_results": state.get("tool_results", []),
                },
                summary=summary,
            )
            await self.memory.extract_and_save_facts(
                UUID(user_id),
                [{"role": "user", "content": state.get("query", "")}],
            )
        except Exception as exc:
            logger.warning("agent.memory_save_failed", error=str(exc))
