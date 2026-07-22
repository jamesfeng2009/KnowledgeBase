"""
工作流 Agent — 单一职责：引导用户完成业务流程。

WorkflowAgent 用于处理需要多步骤、多角色协作的企业业务流程，
如报销流程、请假流程、采购审批等。

执行流程：
1. 理解用户意图，识别业务流程类型；
2. 引导用户逐步完成流程所需的各个步骤；
3. 必要时调用 MCP 工具查询外部系统状态（如 OA 审批进度）。

遵循开闭原则：继承 BaseAgent 获得 Agent Loop 主循环，
只实现 execute 方法。新增流程类型只需扩展 MCP 工具，
不修改 WorkflowAgent 代码。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from app.agents.base import AgentState, BaseAgent
from app.utils.logger import get_logger

logger = get_logger(__name__)


class WorkflowAgent(BaseAgent):
    """工作流 Agent — 引导用户完成业务流程。

    典型场景：
    - 报销流程：引导用户填写报销单 → 查询审批进度 → 回复结果；
    - 请假流程：引导用户说明请假类型与时长 → 提交申请 → 查询审批状态；
    - 采购流程：引导用户填写采购需求 → 提交审批 → 跟踪进度。

    使用方式（通过 AgentRegistry 创建）::

        from app.agents.registry import AgentRegistry

        agent = AgentRegistry.create("workflow", llm, mcp, memory)
        async for chunk in agent.run(query, user_id, session_id):
            print(chunk)
    """

    agent_type: str = "workflow"

    system_prompt: str = (
        "你是一个企业工作流执行助手。请理解用户的业务需求，"
        "引导用户完成对应的业务流程。\n"
        "要求：\n"
        "1. 识别用户意图所属的业务流程类型（如报销、请假、采购等）；\n"
        "2. 明确告知用户需要提供的信息和操作步骤；\n"
        "3. 需要查询外部系统状态时，告知用户正在查询；\n"
        "4. 回答使用中文，步骤清晰，格式规范。"
    )

    async def execute(self, state: AgentState) -> AsyncIterator[str]:
        """执行工作流引导流程。

        步骤：
        1. 分析用户查询，识别可能的业务流程关键词；
        2. 若识别到单据号等查询意图，调用 MCP 查询 OA 审批状态；
        3. 将查询结果作为上下文，调用 LLM 流式生成引导回复。

        Args:
            state: 当前 Agent 状态。

        Yields:
            SSE 格式的文本块。
        """
        query = state.get("query", "")

        # 1. 尝试从查询中提取单据号，查询 OA 审批状态
        bill_no = self._extract_bill_no(query)
        oa_context = ""
        if bill_no:
            oa_result = await self._query_oa_approval(bill_no)
            if oa_result:
                state["tool_results"].append(
                    {"tool": "query_oa_approval", "bill_no": bill_no, "result": oa_result}
                )
                oa_context = f"OA 审批状态查询结果：\n{oa_result}"
                state["messages"].append(
                    {"role": "system", "content": oa_context}
                )

        # 2. 流式生成引导回复
        try:
            async for chunk in self.llm.chat(state["messages"], stream=True):
                if isinstance(chunk, str):
                    yield chunk
        except Exception as exc:
            logger.error("workflow_agent.generate_failed", error=str(exc))
            yield f"抱歉，处理工作流时出现问题：{exc}"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _extract_bill_no(self, query: str) -> str | None:
        """从用户查询中提取单据编号。

        支持的格式（启发式匹配）：
        - BG + 数字（报销单）
        - QJ + 数字（请假单）
        - CG + 数字（采购单）

        Args:
            query: 用户查询文本。

        Returns:
            提取到的单据编号，未匹配到返回 None。
        """
        import re

        # 匹配常见单据编号格式：字母前缀 + 数字
        pattern = r"\b([BQC][GJP]\d{4,})\b"
        match = re.search(pattern, query.upper())
        if match:
            bill_no = match.group(1)
            logger.info("workflow_agent.bill_no_extracted", bill_no=bill_no)
            return bill_no
        return None

    async def _query_oa_approval(self, bill_no: str) -> str | None:
        """通过 MCP query_oa_approval 工具查询 OA 审批状态。

        外部服务不可用时优雅降级，返回 None。

        Args:
            bill_no: 单据编号。

        Returns:
            审批状态查询结果文本，失败时返回 None。
        """
        try:
            result_str = await self.mcp.call_tool(
                "query_oa_approval",
                {"bill_no": bill_no},
            )
            result = json.loads(result_str)
            if "error" in result:
                logger.warning(
                    "workflow_agent.oa_query_error",
                    error=result["error"],
                    bill_no=bill_no,
                )
                return None

            # 格式化审批状态为可读文本
            status = result.get("status", "未知")
            current_node = result.get("current_node", "未知")
            history = result.get("history", [])

            parts = [
                f"单据编号: {bill_no}",
                f"当前状态: {status}",
                f"当前节点: {current_node}",
            ]
            if history:
                parts.append("审批历史:")
                for item in history:
                    parts.append(
                        f"  - {item.get('node', '')} | "
                        f"{item.get('operator', '')} | "
                        f"{item.get('action', '')} | "
                        f"{item.get('time', '')}"
                    )
            return "\n".join(parts)
        except Exception as exc:
            logger.warning("workflow_agent.oa_query_failed", error=str(exc))
            return None
