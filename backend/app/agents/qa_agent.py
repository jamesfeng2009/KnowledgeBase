"""
通用问答 Agent — 单一职责：基于知识库检索的问答执行。

QAAgent 是最常用的 Agent 类型，执行 Agentic RAG 流程：
1. 通过 MCP 工具检索知识库文档；
2. 权限过滤确保用户只看到有权限的内容；
3. 将检索结果作为上下文，调用 LLM 流式生成答案。

遵循开闭原则：继承 BaseAgent 获得 Agent Loop 主循环，
只实现 execute 方法，不修改 think / reflect / run 逻辑。
遵循单一职责：QAAgent 只负责问答场景的检索与生成，
不处理工作流编排或工具执行。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.agents.base import AgentState, BaseAgent
from app.utils.logger import get_logger

logger = get_logger(__name__)


class QAAgent(BaseAgent):
    """通用问答 Agent — Agentic RAG 执行。

    执行流程（execute 方法）：
    1. 调用 MCP knowledge_search 工具检索知识库；
    2. 将检索到的文档作为上下文拼入消息列表；
    3. 调用 LLM 流式生成答案，逐 token yield。

    使用方式（通过 AgentRegistry 创建）::

        from app.agents.registry import AgentRegistry

        agent = AgentRegistry.create("qa", llm, mcp, memory)
        async for chunk in agent.run(query, user_id, session_id):
            print(chunk)
    """

    agent_type: str = "qa"

    system_prompt: str = (
        "你是一个企业知识库问答助手。请基于提供的知识库检索结果，"
        "准确、简洁地回答用户问题。\n"
        "要求：\n"
        "1. 优先引用知识库中的文档内容；\n"
        "2. 若检索结果不足以回答，请如实说明信息不足；\n"
        "3. 回答使用中文，格式清晰。"
    )

    async def execute(self, state: AgentState) -> AsyncIterator[str]:
        """执行 Agentic RAG 流程 — 检索 → 过滤 → 生成。

        步骤：
        1. 通过 MCP knowledge_search 检索知识库文档；
        2. 将检索结果格式化为上下文，追加到消息列表；
        3. 调用 LLM 流式生成答案，逐 token yield。

        Args:
            state: 当前 Agent 状态。

        Yields:
            SSE 格式的文本块。
        """
        query = state.get("query", "")

        # 1. 检索知识库
        retrieved_docs = await self._retrieve(query)
        state["retrieved_docs"] = retrieved_docs

        # 2. 构建上下文并追加到消息列表
        context = self._build_context(retrieved_docs)
        if context:
            # reflect 重试时避免重复追加检索上下文，防止消息数组膨胀与 token 激增
            already_has_context = any(
                msg.get("role") == "system"
                and isinstance(msg.get("content", ""), str)
                and msg.get("content", "").startswith("知识库检索结果：")
                for msg in state["messages"]
            )
            if not already_has_context:
                # 在用户消息前插入检索上下文（作为 system 补充）
                state["messages"].append(
                    {"role": "system", "content": f"知识库检索结果：\n{context}"}
                )

        # 3. 流式生成答案
        answer_parts: list[str] = []
        try:
            async for chunk in self.llm.chat(state["messages"], stream=True):
                if isinstance(chunk, str):
                    answer_parts.append(chunk)
                    yield chunk
        except Exception as exc:
            logger.error("qa_agent.generate_failed", error=str(exc))
            yield f"抱歉，生成回答时出现问题：{exc}"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _retrieve(self, query: str) -> list[dict[str, Any]]:
        """通过 MCP knowledge_search 工具检索知识库文档。

        外部服务不可用时优雅降级，返回空列表。

        Args:
            query: 检索关键词。

        Returns:
            检索到的文档列表（含 id / title / content_preview / kb_id）。
        """
        try:
            result_str = await self.mcp.call_tool(
                "knowledge_search",
                {"query": query},
            )
            result = json.loads(result_str)
            if "error" in result:
                logger.warning(
                    "qa_agent.retrieve_error",
                    error=result["error"],
                )
                return []
            docs = result.get("results", [])
            logger.info(
                "qa_agent.retrieved",
                query=query,
                count=len(docs),
            )
            return docs
        except Exception as exc:
            logger.warning("qa_agent.retrieve_failed", error=str(exc))
            return []

    def _build_context(self, docs: list[dict[str, Any]]) -> str:
        """将检索到的文档列表格式化为上下文文本。

        Args:
            docs: 检索到的文档列表。

        Returns:
            格式化后的上下文文本（各文档标题 + 内容预览）。
        """
        if not docs:
            return ""

        parts: list[str] = []
        for idx, doc in enumerate(docs, 1):
            title = doc.get("title", "未命名文档")
            preview = doc.get("content_preview", "")
            parts.append(f"[文档{idx}] {title}\n{preview}")

        return "\n\n".join(parts)
