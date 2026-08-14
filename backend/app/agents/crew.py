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
        tenant_id: str | None = None,
    ) -> None:
        self.llm = llm
        self.mcp = mcp_client
        self.memory = memory
        self.permission = permission
        self._tenant_id = tenant_id
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
        tenant_id: str | None = None,
    ) -> str | None:
        """执行复杂任务 — CrewAI 拆分 → 多 Agent 协同 → 汇总。

        防传话游戏设计：
            - 原始用户需求作为 original_query 透传字段，不可被覆盖
            - 每个 Agent 的输出包装为结构化 JSON（action_type/result_data/status）
            - 下游 Agent 接收结构化数据而非自然语言总结

        Args:
            query: 用户请求。
            user_id: 用户 ID。
            tenant_id: 租户 ID，覆盖构造时传入的默认值；MCP 工具调用透传该值。

        Returns:
            汇总后的最终回复。如 CrewAI 不可用返回 None。
        """
        if not CREWAI_AVAILABLE:
            logger.warning("crew.not_available_fallback")
            return None

        # 租户 ID 优先级：本次调用 > 构造时默认值
        effective_tenant_id = tenant_id or self._tenant_id

        try:
            # 1. 任务拆分
            sub_tasks = await self._decompose_task(query)
            if not sub_tasks:
                return None

            logger.info("crew.task_decomposed", query=query[:50], sub_tasks=len(sub_tasks))

            # 注入原始查询到子任务（防传话游戏透传字段）：
            # _decompose_task 返回的 LLM JSON 只有 type/description/expected_output，
            # _aggregate_results 从 sub_tasks[0] 读取 original_query，不注入则恒为空串。
            # setdefault 保证已存在时不覆盖。
            for sub_task in sub_tasks:
                sub_task.setdefault("original_query", query)

            # 2. 构建 CrewAI Agent 和 Task
            agents = await self._build_crew_agents(tenant_id=effective_tenant_id)
            tasks = self._build_crew_tasks(sub_tasks, agents, original_query=query)

            # 3. 创建 Crew 并执行
            crew = Crew(
                agents=list(agents.values()),
                tasks=tasks,
                process=Process.sequential,
                verbose=True,
            )

            # kickoff 是同步阻塞调用，放到线程池执行，
            # 避免长时间阻塞 FastAPI 事件循环。
            import asyncio

            result = await asyncio.to_thread(
                crew.kickoff,
                inputs={
                    "user_query": query,
                    "user_id": user_id,
                    "tenant_id": effective_tenant_id,  # 透传租户上下文
                    "original_query": query,  # 必须透传字段，下游 Agent 可读取
                },
            )
            logger.info("crew.execution_complete", result_length=len(str(result)))

            # 4. 结构化结果汇总 — 将各 Agent 输出解析为结构化数据
            structured_result = self._aggregate_results(str(result), sub_tasks)
            return structured_result

        except Exception as e:
            logger.error("crew.execution_error", error=str(e))
            return None

    def _aggregate_results(
        self,
        raw_result: str,
        sub_tasks: list[dict[str, str]],
    ) -> str:
        """将 CrewAI 的原始输出汇总为结构化结果。

        防传话游戏：将自然语言输出解析为结构化 JSON，避免下游转述失真。
        P1-9 对齐：子任务结果采用 plan 步骤模式（step_id/action/description/status）。
        """
        import json as _json

        from app.agents.planner import STEP_DONE, map_task_type_to_action

        structured = {
            "original_query": sub_tasks[0].get("original_query", "") if sub_tasks else "",
            "task_count": len(sub_tasks),
            "results": [],
            "summary": raw_result[:500] if raw_result else "",
        }

        for i, sub in enumerate(sub_tasks):
            structured["results"].append({
                "step_id": i + 1,
                "action": map_task_type_to_action(sub.get("type", "")),
                "type": sub.get("type", "unknown"),
                "description": sub.get("description", ""),
                "expected_output": sub.get("expected_output", ""),
                "status": STEP_DONE,
            })

        try:
            return _json.dumps(structured, ensure_ascii=False, indent=2)
        except Exception:
            return raw_result

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

    async def _build_crew_agents(
        self,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """构建 CrewAI Agent 角色实例。

        P1: 按 Agent 类型筛选工具，避免 QA Agent 拿到写操作工具：
            - QA Agent → 只读工具（knowledge_search / document_get / query_oa_approval）
            - Workflow Agent → 只读工具
            - Action Agent → 全部工具（含 document_create / create_it_ticket）

        Args:
            tenant_id: 租户 ID，透传给 MCP 工具调用。

        Returns:
            {"qa": CrewAgent, "workflow": CrewAgent, "action": CrewAgent}
        """
        if not CREWAI_AVAILABLE:
            return {}

        # 延迟导入工具
        from app.agents.mcp_tools import get_mcp_tools_for_agent_type

        # P1: 按 Agent 类型分别加载工具（而非全量塞入）
        qa_tools = await get_mcp_tools_for_agent_type(self.mcp, "qa", tenant_id=tenant_id)
        workflow_tools = await get_mcp_tools_for_agent_type(self.mcp, "workflow", tenant_id=tenant_id)
        action_tools = await get_mcp_tools_for_agent_type(self.mcp, "action", tenant_id=tenant_id)

        qa_agent = CrewAgent(
            role="知识库问答专家",
            goal="基于企业知识库准确回答用户问题，提供引用来源",
            backstory="你是企业知识库的问答专家，擅长检索和总结信息。",
            llm=self.llm,
            tools=qa_tools,
            verbose=True,
        )
        workflow_agent = CrewAgent(
            role="业务流程引导专家",
            goal="理解用户业务需求，引导完成企业流程",
            backstory="你是企业业务流程专家，了解所有审批流程和单据规则。",
            llm=self.llm,
            tools=workflow_tools,
            verbose=True,
        )
        action_agent = CrewAgent(
            role="执行操作专家",
            goal="将用户指令转化为具体操作，通过工具执行并返回结果",
            backstory="你是执行专家，擅长创建工单、配置系统、执行操作。",
            llm=self.llm,
            tools=action_tools,
            verbose=True,
        )

        return {"qa": qa_agent, "workflow": workflow_agent, "action": action_agent}

    def _build_crew_tasks(
        self,
        sub_tasks: list[dict[str, str]],
        agents: dict[str, Any],
        original_query: str = "",
    ) -> list[Any]:
        """将子任务列表转换为 CrewAI Task 列表。

        防传话游戏设计：
            - 每个任务描述中注入 original_query，确保下游 Agent 拿到用户原始需求
            - 要求 Agent 以结构化 JSON 格式输出，而非自然语言总结

        Args:
            sub_tasks: _decompose_task 返回的子任务列表。
            agents: _build_crew_agents 返回的 Agent 字典。
            original_query: 原始用户需求（必须透传字段）。

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

            # 注入原始需求 + 结构化输出指令
            desc = sub.get("description", "")
            expected = sub.get("expected_output", "相关结果")

            # 防传话游戏：description 中嵌入原始需求，确保不被转述覆盖
            full_desc = desc
            if original_query:
                full_desc = (
                    f"原始用户需求（不可修改，必须参考）：{original_query}\n"
                    f"当前子任务：{desc}\n"
                    f"请完成当前子任务，输出必须包含字段："
                    f'action_type（qa/workflow/action）、'
                    f'result_data（结构化结果）、status（completed/failed）。'
                )

            task = CrewTask(
                description=full_desc,
                agent=agent,
                expected_output=f"结构化 JSON: {expected}",
            )
            tasks.append(task)
        return tasks
