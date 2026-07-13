"""CrewAI 多 Agent 协作 — 复杂任务拆分与多 Agent 协同。

定位：当用户请求复杂、需要多个专业 Agent 协同时，CrewAI 负责：
1. 任务拆分：LLM 分析复杂请求，拆分为子任务
2. Agent 分配：每个子任务分配给最合适的 Agent（QA/Workflow/Action）
3. 顺序执行：前一个 Agent 的输出作为后一个的输入
4. 结果汇总：合并所有 Agent 的输出为最终回复

与 LangGraph 的分工：
  - CrewAI：管多 Agent 之间的协作（项目经理）
  - LangGraph：管单个 Agent 内部的 Loop（个人工作流）

何时触发 CrewAI：
  - 简单任务（"公司报销流程是什么"）→ 直接走单个 Agent
  - 复杂任务（"查报销单 BG001 状态并创建新报销单"）→ CrewAI 拆分 + 多 Agent 协同
"""
from __future__ import annotations

import json
from typing import Any

from app.llm.base import LLMProvider, Message
from app.mcp.client import MCPClient
from app.memory.memory_manager import MemoryManager
from app.services.permission_service import PermissionService
from app.utils.logger import get_logger

logger = get_logger(__name__)

# CrewAI 延迟导入
try:
    from crewai import Agent as CrewAgent, Task as CrewTask, Crew, Process
    CREWAI_AVAILABLE = True
except ImportError:
    CrewAgent = None
    CrewTask = None
    Crew = None
    Process = None
    CREWAI_AVAILABLE = False
    logger.info("crew.crewai_not_installed_fallback")


class KnowledgeBaseCrew:
    """CrewAI 多 Agent 协作编排 — 复杂任务的多 Agent 协同。

    使用方式：
        crew = KnowledgeBaseCrew(llm, mcp_client, memory, permission)
        result = await crew.execute_complex_task(query, user_id)

    决策流程：
    1. LLM 分析请求复杂度，判断是否需要 CrewAI
    2. 如需 CrewAI：拆分任务 → 分配 Agent → 顺序执行 → 汇总
    3. 如不需要：返回 None，由调用方走单个 Agent
    """

    def __init__(
        self,
        llm: LLMProvider,
        mcp_client: MCPClient,
        memory: MemoryManager,
        permission: PermissionService,
    ) -> None:
        self.llm = llm
        self.mcp = mcp_client
        self.memory = memory
        self.permission = permission
        self._crew_agents: dict[str, Any] = {}

    async def should_use_crew(self, query: str) -> bool:
        """判断用户请求是否需要多 Agent 协作。

        简单查询直接走单个 Agent，只有复杂任务才触发 CrewAI。

        判定规则：
        - 包含"并且"/"然后"/"同时"等连接词 → 可能需要
        - 包含多个动作（查询+创建/分析+执行） → 需要
        - 包含跨系统操作（查OA+建工单） → 需要

        Args:
            query: 用户请求。

        Returns:
            True 表示需要 CrewAI 协作。
        """
        # 启发式规则：快速判断
        complexity_keywords = ["并且", "然后", "同时", "接着", "之后", "再", "还要"]
        action_keywords = ["查询", "创建", "提交", "审批", "分析", "生成", "修改", "删除"]

        has_connector = any(kw in query for kw in complexity_keywords)
        action_count = sum(1 for kw in action_keywords if kw in query)

        if has_connector and action_count >= 2:
            return True
        if action_count >= 3:
            return True

        # LLM 精确判断（可选，减少 LLM 调用）
        try:
            prompt = (
                "判断以下用户请求是否需要多个步骤/多个系统协作才能完成。\n"
                "只回复 'yes' 或 'no'。\n"
                f"用户请求：{query}"
            )
            text = ""
            async for chunk in self.llm.chat(
                [Message(role="system", content=prompt)],
                stream=False,
                max_tokens=10,
            ):
                if isinstance(chunk, str):
                    text += chunk
            return "yes" in text.strip().lower()
        except Exception as e:
            logger.warning("crew.should_use_crew_fallback", error=str(e))
            return False

    async def execute_complex_task(
        self,
        query: str,
        user_id: str,
    ) -> str | None:
        """执行复杂任务 — CrewAI 拆分 → 多 Agent 协同 → 汇总。

        Args:
            query: 用户请求。
            user_id: 用户 ID。

        Returns:
            汇总后的最终回复。如 CrewAI 不可用返回 None。
        """
        if not CREWAI_AVAILABLE:
            logger.warning("crew.not_available_fallback")
            return None

        try:
            # 1. 任务拆分
            sub_tasks = await self._decompose_task(query)
            if not sub_tasks:
                return None

            logger.info("crew.task_decomposed", query=query[:50], sub_tasks=len(sub_tasks))

            # 2. 构建 CrewAI Agent 和 Task
            agents = self._build_crew_agents()
            tasks = self._build_crew_tasks(sub_tasks, agents)

            # 3. 创建 Crew 并执行
            crew = Crew(
                agents=list(agents.values()),
                tasks=tasks,
                process=Process.sequential,
                verbose=True,
            )

            result = crew.kickoff(inputs={"user_query": query, "user_id": user_id})
            logger.info("crew.execution_complete", result_length=len(str(result)))
            return str(result)

        except Exception as e:
            logger.error("crew.execution_error", error=str(e))
            return None

    async def _decompose_task(self, query: str) -> list[dict[str, str]]:
        """LLM 分析复杂任务，拆分为子任务列表。

        Returns:
            子任务列表：[{type, description, expected_output}]
            type: qa / workflow / action
        """
        prompt = f"""分析以下用户请求，拆分为有序的子任务。
每个子任务标注类型：
- qa：知识检索（查文档、查政策）
- workflow：流程引导（查审批状态、查进度）
- action：执行操作（创建单据、提交工单）

以 JSON 数组格式返回，每个元素包含 type, description, expected_output。
用户请求：{query}

示例输出：
[
  {{"type": "qa", "description": "检索公司报销政策文档", "expected_output": "报销政策摘要"}},
  {{"type": "workflow", "description": "查询报销单 BG001 审批状态", "expected_output": "审批进度信息"}},
  {{"type": "action", "description": "创建新报销单", "expected_output": "新报销单号"}}
]
"""
        try:
            text = ""
            async for chunk in self.llm.chat(
                [
                    Message(role="system", content="你是任务分析专家。"),
                    Message(role="user", content=prompt),
                ],
                stream=False,
            ):
                if isinstance(chunk, str):
                    text += chunk

            # 解析 JSON
            import re

            json_match = re.search(r"\[.*\]", text, re.DOTALL)
            if json_match:
                tasks = json.loads(json_match.group())
                return tasks
            return []
        except Exception as e:
            logger.error("crew.decompose_error", error=str(e))
            return []

    def _build_crew_agents(self) -> dict[str, Any]:
        """构建 CrewAI Agent 角色实例。

        Returns:
            {"qa": CrewAgent, "workflow": CrewAgent, "action": CrewAgent}
        """
        if not CREWAI_AVAILABLE:
            return {}

        # 延迟导入工具
        from app.agents.mcp_tools import get_mcp_tools_for_crewai
        import asyncio

        # 获取 MCP 工具（同步等待）
        loop = asyncio.get_event_loop()
        tools = loop.run_until_complete(get_mcp_tools_for_crewai(self.mcp))

        qa_agent = CrewAgent(
            role="知识库问答专家",
            goal="基于企业知识库准确回答用户问题，提供引用来源",
            backstory="你是企业知识库的问答专家，擅长检索和总结信息。",
            llm=self.llm,
            tools=tools,
            verbose=True,
        )
        workflow_agent = CrewAgent(
            role="业务流程引导专家",
            goal="理解用户业务需求，引导完成企业流程",
            backstory="你是企业业务流程专家，了解所有审批流程和单据规则。",
            llm=self.llm,
            tools=tools,
            verbose=True,
        )
        action_agent = CrewAgent(
            role="执行操作专家",
            goal="将用户指令转化为具体操作，通过工具执行并返回结果",
            backstory="你是执行专家，擅长创建工单、配置系统、执行操作。",
            llm=self.llm,
            tools=tools,
            verbose=True,
        )

        return {"qa": qa_agent, "workflow": workflow_agent, "action": action_agent}

    def _build_crew_tasks(
        self,
        sub_tasks: list[dict[str, str]],
        agents: dict[str, Any],
    ) -> list[Any]:
        """将子任务列表转换为 CrewAI Task 列表。

        Args:
            sub_tasks: _decompose_task 返回的子任务列表。
            agents: _build_crew_agents 返回的 Agent 字典。

        Returns:
            CrewTask 列表（顺序执行）。
        """
        if not CREWAI_AVAILABLE:
            return []

        tasks = []
        for sub in sub_tasks:
            task_type = sub.get("type", "qa")
            agent = agents.get(task_type, agents.get("qa"))
            if agent is None:
                continue
            task = CrewTask(
                description=sub.get("description", ""),
                agent=agent,
                expected_output=sub.get("expected_output", "相关结果"),
            )
            tasks.append(task)
        return tasks
