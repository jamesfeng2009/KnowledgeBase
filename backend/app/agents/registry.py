"""
Agent 注册表 — 单一职责：Agent 类型的注册与创建。

采用注册表（registry + decorator）模式实现开闭原则：
新增 Agent 类型只需用 @register 装饰新类，
AgentRegistry.create 会自动发现并创建对应实例，
无需修改 create 方法内部分支逻辑。

遵循单一职责：AgentRegistry 只负责注册与创建，
不包含 Agent 的业务逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from app.agents.base import BaseAgent
from app.agents.action_agent import ActionAgent
from app.agents.qa_agent import QAAgent
from app.agents.workflow_agent import WorkflowAgent
from app.llm.base import LLMProvider
from app.mcp.client import MCPClient
from app.memory.memory_manager import MemoryManager
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: 装饰器类型别名 — 接受 Agent 类并返回 Agent 类的装饰器。
AgentDecorator: TypeAlias = Callable[[type[BaseAgent]], type[BaseAgent]]


class AgentRegistry:
    """Agent 注册表 — Agent 类型的注册、发现与创建。

    使用方式::

        # 1. 注册自定义 Agent（使用装饰器）
        @AgentRegistry.register("custom")
        class CustomAgent(BaseAgent):
            ...

        # 2. 创建 Agent 实例
        agent = AgentRegistry.create("qa", llm, mcp, memory)

    开闭原则落点：新增 Agent 类型只需新增一个被 @register 装饰的类，
    不触碰 create 方法与既有注册项。
    """

    #: 注册表 — agent_type → Agent 类。
    _registry: dict[str, type[BaseAgent]] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    @classmethod
    def register(
        cls,
        agent_type: str,
    ) -> AgentDecorator:
        """装饰器：注册 Agent 类型。

        开闭原则落点：新增 Agent 类型只需用此装饰器标注一个新类，
        AgentRegistry.create 会自动发现并创建对应实例。

        Args:
            agent_type: Agent 类型标识（如 "qa" / "workflow" / "action"）。

        Returns:
            类装饰器（原样返回被装饰的类）。
        """

        def decorator(
            agent_cls: type[BaseAgent],
        ) -> type[BaseAgent]:
            cls._registry[agent_type] = agent_cls
            logger.info(
                "agent.registered",
                agent_type=agent_type,
                class_name=agent_cls.__name__,
            )
            return agent_cls

        return decorator

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        agent_type: str,
        llm: LLMProvider,
        mcp: MCPClient,
        memory: MemoryManager,
    ) -> BaseAgent:
        """根据类型创建 Agent 实例。

        Args:
            agent_type: Agent 类型标识。
            llm: LLM Provider 实例。
            mcp: MCP 客户端实例。
            memory: 记忆管理器实例。

        Returns:
            对应类型的 BaseAgent 实例。

        Raises:
            ValueError: agent_type 未在注册表中。
        """
        agent_cls = cls._registry.get(agent_type)
        if agent_cls is None:
            raise ValueError(
                f"未注册的 Agent 类型: {agent_type}，"
                f"已注册: {list(cls._registry)}"
            )
        logger.info(
            "agent.creating",
            agent_type=agent_type,
            class_name=agent_cls.__name__,
        )
        return agent_cls(llm, mcp, memory)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @classmethod
    def list_agents(cls) -> dict[str, str]:
        """返回已注册的 Agent 类型映射（调试/可观测用）。

        Returns:
            agent_type → 类名 的字典。
        """
        return {
            agent_type: agent_cls.__name__
            for agent_type, agent_cls in cls._registry.items()
        }

    @classmethod
    def is_registered(cls, agent_type: str) -> bool:
        """检查指定类型是否已注册。"""
        return agent_type in cls._registry


# ------------------------------------------------------------------
# 自动注册内置 Agent 类型
# ------------------------------------------------------------------

AgentRegistry.register("qa")(QAAgent)
AgentRegistry.register("workflow")(WorkflowAgent)
AgentRegistry.register("action")(ActionAgent)
