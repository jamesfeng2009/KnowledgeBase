"""
执行型 Agent — 单一职责：将用户指令转化为可执行操作并返回结果。

ActionAgent 用于处理需要实际执行操作的场景，如：
- IT 运维工单创建；
- 系统配置变更；
- 权限申请与变更。

执行流程：
1. 分析用户指令，识别可执行的操作类型；
2. 将指令拆解为具体步骤；
3. 通过 MCP 调用对应工具执行操作；
4. 汇总执行结果，流式返回。

遵循开闭原则：继承 BaseAgent 获得 Agent Loop 主循环，
只实现 execute 方法。新增操作类型只需扩展 MCP 工具。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from app.agents.base import AgentState, BaseAgent
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ActionAgent(BaseAgent):
    """执行型 Agent — 将指令转化为可执行步骤并通过 MCP 工具执行。

    典型场景：
    - IT 运维：用户描述问题 → 创建 IT 工单 → 返回工单号；
    - 系统配置：用户描述配置需求 → 调用配置工具 → 返回执行结果；
    - 权限申请：用户申请权限 → 提交审批流程 → 返回流程信息。

    使用方式（通过 AgentRegistry 创建）::

        from app.agents.registry import AgentRegistry

        agent = AgentRegistry.create("action", llm, mcp, memory)
        async for chunk in agent.run(query, user_id, session_id):
            print(chunk)
    """

    agent_type: str = "action"

    system_prompt: str = (
        "你是一个行动执行助手。请将用户的指令转化为具体可执行的步骤，"
        "并协助用户完成操作。\n"
        "要求：\n"
        "1. 分析用户指令，识别需要执行的操作类型；\n"
        "2. 将复杂指令拆解为清晰的步骤列表；\n"
        "3. 能通过工具自动完成的操作，直接执行并返回结果；\n"
        "4. 需要用户手动操作的，给出明确的操作指引；\n"
        "5. 回答使用中文，步骤明确，格式规范。"
    )

    async def execute(self, state: AgentState) -> AsyncIterator[str]:
        """执行操作流程 — 分析意图 → 调用工具 → 生成结果。

        步骤：
        1. 分析用户查询，识别可执行的操作意图；
        2. 若识别到 IT 工单创建意图，调用 MCP create_it_ticket 工具；
        3. 将工具执行结果作为上下文，调用 LLM 流式生成回复。

        Args:
            state: 当前 Agent 状态。

        Yields:
            SSE 格式的文本块。
        """
        query = state.get("query", "")

        # 1. 识别操作意图并执行对应工具
        action_context = await self._try_execute_action(query, state)
        if action_context:
            state["messages"].append(
                {"role": "system", "content": action_context}
            )

        # 2. 流式生成回复（含执行结果摘要）
        try:
            async for chunk in self.llm.chat(state["messages"], stream=True):
                if isinstance(chunk, str):
                    yield chunk
        except Exception as exc:
            logger.error("action_agent.generate_failed", error=str(exc))
            yield f"抱歉，执行操作时出现问题：{exc}"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _try_execute_action(
        self,
        query: str,
        state: AgentState,
    ) -> str | None:
        """分析用户意图并尝试执行对应工具。

        当前支持的操作：
        - 创建 IT 工单（检测到"工单"/"故障"/"报修"等关键词时触发）；
        - 未来可扩展更多操作类型（通过扩展此方法或子类覆盖）。

        Args:
            query: 用户查询文本。
            state: 当前 Agent 状态（用于记录工具调用结果）。

        Returns:
            工具执行结果格式化后的上下文文本，未执行操作时返回 None。
        """
        # 检测 IT 工单创建意图
        if self._detect_it_ticket_intent(query):
            return await self._create_it_ticket(query, state)

        return None

    def _detect_it_ticket_intent(self, query: str) -> bool:
        """检测用户查询中是否包含 IT 工单创建意图。

        启发式关键词匹配：工单、故障、报修、报错、无法访问、系统异常等。

        Args:
            query: 用户查询文本。

        Returns:
            True 表示检测到 IT 工单意图。
        """
        keywords = [
            "工单", "故障", "报修", "报错", "无法访问",
            "系统异常", "系统崩溃", "网络问题", "密码重置",
            "账号锁定", "权限不足",
        ]
        query_lower = query.lower()
        return any(kw in query for kw in keywords) or any(
            kw in query_lower for kw in keywords
        )

    async def _create_it_ticket(
        self,
        query: str,
        state: AgentState,
    ) -> str | None:
        """通过 MCP create_it_ticket 工具创建 IT 工单。

        外部服务不可用时优雅降级，返回 None。

        Args:
            query: 用户查询文本（作为工单描述）。
            state: 当前 Agent 状态（用于记录工具调用结果）。

        Returns:
            工单创建结果格式化后的上下文文本，失败时返回 None。
        """
        try:
            # 从查询中提取标题（取前 30 字符）
            title = query[:30] if len(query) > 30 else query
            result_str = await self.mcp.call_tool(
                "create_it_ticket",
                {
                    "title": title,
                    "description": query,
                    "priority": "normal",
                },
            )
            result = json.loads(result_str)
            if "error" in result:
                logger.warning(
                    "action_agent.it_ticket_error",
                    error=result["error"],
                )
                return None

            state["tool_results"].append(
                {"tool": "create_it_ticket", "result": result}
            )

            ticket_id = result.get("ticket_id", "未知")
            priority = result.get("priority", "normal")
            status = result.get("status", "open")

            context = (
                f"IT 工单已创建：\n"
                f"  工单号: {ticket_id}\n"
                f"  优先级: {priority}\n"
                f"  状态: {status}\n"
                f"请记录工单号以便后续查询进度。"
            )
            logger.info(
                "action_agent.it_ticket_created",
                ticket_id=ticket_id,
            )
            return context
        except Exception as exc:
            logger.warning("action_agent.it_ticket_failed", error=str(exc))
            return None
