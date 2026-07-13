"""
Agentic RAG 主引擎 — 单一职责：编排 think → retrieve/tool_call → generate → reflect 循环。

默认采用纯 Python 实现的 Agent Loop（不依赖 langgraph 库），通过 ``while`` 循环 +
条件分支驱动状态流转，更简单且无外部依赖。

当安装 ``langgraph`` 后，可通过 ``build_graph()`` / ``answer_with_graph()`` 切换为
声明式状态图驱动，获得断点恢复（PostgresSaver）与图可视化等能力；未安装时回退到
默认的 ``answer()`` 路径，功能完全等价。

循环结构::

    think → [retrieve | tool_call] → think → ... → generate → reflect
                                                          ↓
                                              satisfied → END
                                              retry → think

关键设计：
    - ``max_iterations`` 防止无限循环（默认 5 次）；
    - **权限过滤在重排之前**（核心安全约束）：检索召回 → ABAC 权限过滤 → 重排；
    - ``answer()`` / ``answer_with_graph()`` 均返回 ``AsyncIterator[str]`` 供 SSE 流式消费；
    - 工具调用通过 MCPClient 转发，不耦合具体工具实现。

遵循单一职责：本模块只负责流程编排，检索/重排/生成/权限均委托注入的组件。
遵循依赖倒置：LLM、检索器、重排器、权限过滤器均通过构造注入，可替换为 Mock。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypedDict

from app.llm.base import LLMProvider, Message, ToolUse
from app.mcp.client import MCPClient
from app.observability.langfuse_tracer import TraceContext, trace_node
from app.rag.cache import TokenCache
from app.rag.generator import Generator
from app.rag.reranker import RerankerBase
from app.rag.retriever import HybridRetriever
from app.utils.logger import get_logger

# 延迟导入 LangGraph（可能未安装）— 安装时可通过 build_graph() / answer_with_graph()
# 使用声明式状态图驱动 Agent Loop，并支持 PostgresSaver 断点恢复；
# 未安装时回退到默认的纯 Python answer() 路径，无任何功能损失。
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.postgres import PostgresSaver

    LANGGRAPH_AVAILABLE: bool = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = None  # type: ignore[assignment, misc]
    END = None  # type: ignore[assignment, misc]
    PostgresSaver = None  # type: ignore[assignment, misc]

log = get_logger(__name__)

# 默认最大迭代次数
_DEFAULT_MAX_ITERATIONS: int = 5
# 重排默认 top_k
_RERANK_TOP_K: int = 5
# 检索默认 top_k
_RETRIEVE_TOP_K: int = 20

# P0-Opt2: 稳定 system prompt — 不含动态内容（迭代计数/文档数/工具数），
# 使前缀字节稳定以命中 Anthropic KV Cache。
# 动态状态作为 "live zone" 消息在每轮 think 中追加，不嵌入 system prompt。
_THINK_SYSTEM_STABLE: str = (
    "你是企业知识库助手的决策大脑。分析用户问题和已有信息，决定下一步：\n"
    '- 回复 "retrieve"：需要检索知识库补充信息；\n'
    '- 回复 "tool_call"：需要调用企业系统工具（如查 OA/ERP/IT 工单）；\n'
    '- 回复 "generate"：已有足够信息，可以生成最终答案。\n\n'
    "只回复上述三个关键词之一，不要附加解释。"
)

# 权限过滤器类型 — 接收候选文档列表，返回过滤后的列表。
# 由调用方注入（通常封装 PermissionService.filter_documents 对 dict 的适配）。
PermissionFilter = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]


class AgentState(TypedDict, total=False):
    """Agent Loop 状态 — 在循环各节点间传递。

    Attributes:
        query: 用户原始问题。
        user_id: 当前用户 ID。
        session_id: 会话 ID（用于记忆与缓存隔离）。
        messages: 累积的消息列表（system / user / assistant / tool）。
        retrieved_docs: 检索并重排后的文档列表。
        tool_results: MCP 工具调用结果列表。
        answer: 生成的最终答案。
        iteration: 当前迭代轮次。
        max_iterations: 最大迭代次数（防无限循环）。
        kb_ids: 可选，限定检索的知识库范围。
        memory_context: 记忆引擎提供的上下文。
    """

    query: str
    user_id: str
    session_id: str
    messages: list[Message]
    retrieved_docs: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    answer: str
    iteration: int
    max_iterations: int
    kb_ids: list[str] | None
    memory_context: str
    # --- LangGraph 专用字段（纯 Python 路径不使用）---
    # think 节点产出的路由信号：retrieve / tool_call / generate。
    _decision: str
    # generate 节点产出的逐 token 片段，供 answer_with_graph 流式回放。
    _stream_tokens: list[str]


class AgenticRAGEngine:
    """Agentic RAG 引擎 — 纯 Python Agent Loop 实现（可选 LangGraph 状态图）。

    使用方式::

        engine = AgenticRAGEngine(
            llm=get_llm_provider(),
            mcp_client=mcp_client,
            retriever=HybridRetriever(),
            reranker=get_reranker(),
            generator=Generator(get_llm_provider()),
            permission_filter=my_filter,
        )
        # 默认路径（纯 Python while 循环）
        async for token in engine.answer(query, user_id, session_id):
            yield token  # SSE 流式输出

    LangGraph 可选路径（安装 langgraph 后）::

        from langgraph.checkpoint.postgres import PostgresSaver
        checkpointer = PostgresSaver(async_connection_string)
        engine = AgenticRAGEngine(..., checkpointer=checkpointer)
        async for token in engine.answer_with_graph(query, user_id, session_id):
            yield token  # 支持断点恢复
    """

    def __init__(
        self,
        llm: LLMProvider,
        mcp_client: MCPClient,
        retriever: HybridRetriever,
        reranker: RerankerBase,
        generator: Generator,
        cache: TokenCache | None = None,
        permission_filter: PermissionFilter | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        checkpointer: Any = None,
    ) -> None:
        self.llm = llm
        self.mcp = mcp_client
        self.retriever = retriever
        self.reranker = reranker
        self.generator = generator
        self.cache = cache
        self.permission_filter = permission_filter
        self.max_iterations = max_iterations
        # LangGraph 断点保存器（可选）— 传入 PostgresSaver 实例后，
        # build_graph() 会将其编译进状态图，支持中断恢复。
        self._checkpointer = checkpointer
        # 编译后的状态图缓存（首次 build_graph 后复用）。
        self._compiled_graph: Any = None
        # LangFuse 追踪上下文（每次 answer 调用时重置）
        self._trace_ctx: TraceContext | None = None

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def answer(
        self,
        query: str,
        user_id: str,
        session_id: str,
        kb_ids: list[str] | None = None,
        memory_context: str = "",
    ) -> AsyncIterator[str]:
        """Agentic RAG 主入口 — 返回答案 token 流供 SSE 消费。

        流程：
            1. 查询 Token 缓存，命中则直接返回；
            2. 执行 Agent Loop（think → retrieve/tool_call → generate → reflect）；
            3. 生成阶段流式 yield token；
            4. 生成完成后回写缓存。

        Args:
            query: 用户问题。
            user_id: 当前用户 ID（用于权限过滤）。
            session_id: 会话 ID。
            kb_ids: 可选，限定检索的知识库范围。
            memory_context: 记忆引擎提供的上下文。

        Yields:
            str: 答案文本片段。
        """
        # 1. 缓存命中检查
        if self.cache is not None:
            try:
                cached = await self.cache.get(query)
                if cached is not None:
                    log.info("engine.cache.hit", session_id=session_id)
                    yield cached
                    return
            except Exception as exc:
                log.warning("engine.cache.get_error", error=str(exc))

        # 2. 初始化状态
        state: AgentState = {
            "query": query,
            "user_id": user_id,
            "session_id": session_id,
            "messages": [],
            "retrieved_docs": [],
            "tool_results": [],
            "answer": "",
            "iteration": 0,
            "max_iterations": self.max_iterations,
            "kb_ids": kb_ids,
            "memory_context": memory_context,
        }

        # 2.5 初始化 LangFuse 追踪
        self._trace_ctx = TraceContext(
            trace_name="rag_agent_loop",
            session_id=session_id,
            user_id=user_id,
            metadata={"query": query[:200]},
        )
        self._trace_ctx.start()

        # 3. 执行 think / retrieve / tool_call 循环（非流式），直到决定生成
        await self._run_decision_loop(state)

        # 4. 流式生成答案
        answer_parts: list[str] = []
        async for token in self.generator.generate(
            query=state["query"],
            retrieved_docs=state["retrieved_docs"],
            tool_results=state["tool_results"],
            memory_context=state.get("memory_context", ""),
        ):
            answer_parts.append(token)
            yield token

        # 5. 反思（非流式，仅记录结果）
        answer = "".join(answer_parts)
        state["answer"] = answer
        await self._reflect(state)

        # 6. 回写缓存
        if self.cache is not None and answer:
            try:
                await self.cache.set(query, answer)
            except Exception as exc:
                log.warning("engine.cache.set_error", error=str(exc))

        # 7. 结束 Trace
        if self._trace_ctx is not None:
            self._trace_ctx.finalize(
                output=answer[:500],
                metadata={
                    "iterations": state["iteration"],
                    "total_tokens": len(answer) // 4,  # 粗估
                    "retrieved_docs": len(state["retrieved_docs"]),
                    "tool_results": len(state["tool_results"]),
                },
            )

    # ------------------------------------------------------------------
    # LangGraph 可选路径 — 声明式状态图 + 断点恢复
    # ------------------------------------------------------------------
    # 以下方法仅在 LANGGRAPH_AVAILABLE 时可用；未安装 LangGraph 时调用
    # 会抛出 RuntimeError，调用方应回退到默认的 answer() 路径。
    # 图结构与纯 Python Agent Loop 完全等价：
    #   START → think → [retrieve | tool_call | generate] → ... → generate → reflect → END

    def build_graph(self) -> Any:
        """构建并编译 LangGraph 状态图（声明式 Agent Loop）。

        将 think → retrieve/tool_call → generate → reflect 的循环
        声明式化为 StateGraph，节点间通过条件边路由，可附加 checkpointer
        实现中断恢复。图结构::

            START → think ──┬─ retrieve ─→ think（循环）
                             ├─ tool_call ─→ think（循环）
                             └─ generate  ─→ reflect ─→ END

        Returns:
            编译后的可执行图（CompiledGraph）。

        Raises:
            RuntimeError: LangGraph 未安装时抛出。
        """
        if not LANGGRAPH_AVAILABLE:
            raise RuntimeError(
                "LangGraph not installed — 使用 pip install langgraph 后重试，"
                "或回退到默认的 answer() 方法。"
            )

        graph: Any = StateGraph(AgentState)

        # 注册节点 — 复用现有私有方法的逻辑
        graph.add_node("think", self._graph_think)
        graph.add_node("retrieve", self._graph_retrieve)
        graph.add_node("tool_call", self._graph_tool_call)
        graph.add_node("generate", self._graph_generate)
        graph.add_node("reflect", self._graph_reflect)

        # 入口边
        graph.set_entry_point("think")

        # think → 条件路由（retrieve / tool_call / generate）
        graph.add_conditional_edges(
            "think",
            self._route_after_think,
            {
                "retrieve": "retrieve",
                "tool_call": "tool_call",
                "generate": "generate",
            },
        )
        # retrieve / tool_call 完成后回到 think 继续循环
        graph.add_edge("retrieve", "think")
        graph.add_edge("tool_call", "think")
        # generate → reflect → END
        graph.add_edge("generate", "reflect")
        graph.add_edge("reflect", END)

        compiled = graph.compile(checkpointer=self._checkpointer)
        self._compiled_graph = compiled
        log.info("engine.graph.built", checkpointer=self._checkpointer is not None)
        return compiled

    def _get_or_build_graph(self) -> Any:
        """获取已编译的图（复用缓存，避免重复编译）。"""
        if self._compiled_graph is None:
            self.build_graph()
        return self._compiled_graph

    async def answer_with_graph(
        self,
        query: str,
        user_id: str,
        session_id: str,
        kb_ids: list[str] | None = None,
        memory_context: str = "",
    ) -> AsyncIterator[str]:
        """LangGraph 驱动的 RAG 入口 — 返回答案 token 流。

        与 ``answer()`` 的区别：使用编译后的 StateGraph 驱动 Agent Loop，
        支持 checkpointer 断点恢复（如 PostgresSaver）。逻辑与 answer() 等价，
        但路由由图边声明式完成，而非 while 循环。

        Args:
            query: 用户问题。
            user_id: 当前用户 ID（用于权限过滤）。
            session_id: 会话 ID（同时用作 LangGraph thread_id）。
            kb_ids: 可选，限定检索的知识库范围。
            memory_context: 记忆引擎提供的上下文。

        Yields:
            str: 答案文本片段。
        """
        if not LANGGRAPH_AVAILABLE:
            raise RuntimeError(
                "LangGraph not installed — 请回退到默认的 answer() 方法。"
            )

        # 1. 缓存命中检查（与 answer() 行为一致）
        if self.cache is not None:
            try:
                cached = await self.cache.get(query)
                if cached is not None:
                    log.info("engine.cache.hit", session_id=session_id)
                    yield cached
                    return
            except Exception as exc:
                log.warning("engine.cache.get_error", error=str(exc))

        # 2. 初始化状态
        state: AgentState = {
            "query": query,
            "user_id": user_id,
            "session_id": session_id,
            "messages": [],
            "retrieved_docs": [],
            "tool_results": [],
            "answer": "",
            "iteration": 0,
            "max_iterations": self.max_iterations,
            "kb_ids": kb_ids,
            "memory_context": memory_context,
            "_decision": "",
            "_stream_tokens": [],
        }

        # 3. 执行状态图（流式捕获 generate 节点的 token）
        compiled = self._get_or_build_graph()
        config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
        answer_parts: list[str] = []

        try:
            async for output in compiled.astream(
                state,
                config=config,
                stream_mode="updates",
            ):
                # stream_mode="updates" 每个节点完成后 yield 该节点的状态增量。
                # 不同 LangGraph 版本返回 dict 或 (node_name, update) 元组，统一规整。
                update: dict[str, Any] = (
                    output
                    if isinstance(output, dict)
                    else output[1]
                    if isinstance(output, tuple) and len(output) >= 2
                    else {}
                )
                if not isinstance(update, dict):
                    continue
                # generate 节点完成后，逐 token 回放给消费者
                tokens = update.get("_stream_tokens")
                if tokens:
                    for token in tokens:
                        answer_parts.append(token)
                        yield token
        except Exception as exc:
            log.error("engine.graph.stream_error", error=str(exc))
            if answer_parts:
                return
            yield f"[Graph 执行出错: {exc}]"
            return

        # 4. 回写缓存
        answer = "".join(answer_parts)
        if self.cache is not None and answer:
            try:
                await self.cache.set(query, answer)
            except Exception as exc:
                log.warning("engine.cache.set_error", error=str(exc))

    # ------------------------------------------------------------------
    # LangGraph 节点实现 — 复用现有 _think / _retrieve / _tool_call / _reflect
    # ------------------------------------------------------------------

    async def _graph_think(self, state: AgentState) -> dict[str, Any]:
        """think 节点 — 递增迭代计数并决策下一步路由。

        返回 ``_decision`` 供条件边读取；超限时强制路由到 generate。
        """
        iteration = state.get("iteration", 0) + 1
        max_iter = state.get("max_iterations", self.max_iterations)
        state["iteration"] = iteration

        if iteration > max_iter:
            log.info(
                "engine.graph.max_iterations",
                iteration=iteration,
                session_id=state.get("session_id", ""),
            )
            decision = "generate"
        else:
            decision = await self._think(state)

        return {"iteration": iteration, "_decision": decision}

    @staticmethod
    def _route_after_think(state: AgentState) -> str:
        """条件路由函数 — 读取 think 节点产出的 ``_decision`` 决定下一节点。"""
        decision = state.get("_decision", "generate")
        if decision in ("retrieve", "tool_call", "generate"):
            return decision
        return "generate"

    async def _graph_retrieve(self, state: AgentState) -> dict[str, Any]:
        """retrieve 节点 — 委托现有 ``_retrieve`` 完成检索 → 权限过滤 → 重排。"""
        await self._retrieve(state, state.get("kb_ids"))
        return {"retrieved_docs": state.get("retrieved_docs", [])}

    async def _graph_tool_call(self, state: AgentState) -> dict[str, Any]:
        """tool_call 节点 — 委托现有 ``_tool_call`` 通过 MCPClient 调用工具。"""
        await self._tool_call(state)
        return {"tool_results": state.get("tool_results", [])}

    async def _graph_generate(self, state: AgentState) -> dict[str, Any]:
        """generate 节点 — 流式生成答案并收集 token 供 answer_with_graph 回放。

        将 ``Generator.generate`` 的逐 token 输出收集到 ``_stream_tokens``，
        同时拼接为完整答案写入 ``answer``。
        """
        tokens: list[str] = []
        async for token in self.generator.generate(
            query=state.get("query", ""),
            retrieved_docs=state.get("retrieved_docs", []),
            tool_results=state.get("tool_results", []),
            memory_context=state.get("memory_context", ""),
        ):
            tokens.append(token)

        return {
            "answer": "".join(tokens),
            "_stream_tokens": tokens,
        }

    async def _graph_reflect(self, state: AgentState) -> dict[str, Any]:
        """reflect 节点 — 委托现有 ``_reflect`` 进行自我反思（非流式）。"""
        state["answer"] = state.get("answer", "")
        await self._reflect(state)
        return {}

    # ------------------------------------------------------------------
    # 决策循环（纯 Python while + 条件分支）
    # ------------------------------------------------------------------

    async def _run_decision_loop(self, state: AgentState) -> None:
        """执行 think → retrieve/tool_call 的循环，直到决定生成或达到上限。

        这是 Agent Loop 的核心：用 ``while`` 循环替代 LangGraph 的图边，
        通过 ``_think`` 返回的路由信号决定下一步动作。

        P0-Opt2: Live-Zone 增量上下文传递 — 循环开始前初始化稳定前缀
        [system_stable, user_query]，后续每轮只追加增量结果（短摘要），
        不重建完整 messages 列表。前缀字节稳定以命中 Anthropic KV Cache。
        """
        kb_ids = state.get("kb_ids")

        # P0-Opt2: 初始化稳定前缀 — system prompt（无动态内容）+ user query
        # 此前缀在整个循环中不修改，保证 Anthropic Prompt Cache 命中。
        state["messages"] = [
            {"role": "system", "content": _THINK_SYSTEM_STABLE},
            {"role": "user", "content": state["query"]},
        ]

        while True:
            state["iteration"] += 1
            if state["iteration"] > state["max_iterations"]:
                log.info(
                    "engine.max_iterations_reached",
                    iteration=state["iteration"],
                    session_id=state["session_id"],
                )
                break

            # think：LLM 决策下一步（读取 state["messages"] 稳定前缀 + 增量结果）
            decision = await self._think(state)

            if decision == "retrieve":
                await self._retrieve(state, kb_ids)
                # P0-Opt2: 追加增量结果摘要（非重建），前缀保持稳定
                state["messages"].append(
                    {
                        "role": "user",
                        "content": f"[系统] 已检索到 {len(state['retrieved_docs'])} 篇文档",
                    }
                )
                continue
            if decision == "tool_call":
                await self._tool_call(state)
                # P0-Opt2: 只追加最新工具结果摘要，不重传历史结果
                if state["tool_results"]:
                    latest = state["tool_results"][-1]
                    state["messages"].append(
                        {
                            "role": "user",
                            "content": f"[系统] 工具结果：{self._summarize(latest)}",
                        }
                    )
                continue
            # decision == "generate" 或其他 → 退出循环进入生成
            log.info(
                "engine.decision_generate",
                iteration=state["iteration"],
                session_id=state["session_id"],
            )
            break

    # ------------------------------------------------------------------
    # think：LLM 决策
    # ------------------------------------------------------------------

    @trace_node("think")
    async def _think(self, state: AgentState) -> str:
        """LLM 决策下一步动作：retrieve / tool_call / generate。

        返回值即路由信号，供 ``_run_decision_loop`` 分支处理。

        P0-Opt2: 使用 state["messages"] 作为稳定前缀（由 _run_decision_loop
        初始化），不重建完整 messages 列表。动态状态（迭代计数/文档数/工具数）
        作为短消息追加到末尾（"live zone"），不嵌入 system prompt。

        向后兼容：若 state["messages"] 为空（直接调用 _think 而非通过
        _run_decision_loop），则从稳定 prompt + query 构建。
        """
        # P0-Opt2: 读取稳定前缀（system_stable + user_query + 增量结果摘要）
        base_messages = state.get("messages", [])

        # 向后兼容：messages 为空时从稳定 prompt 构建
        if not base_messages:
            base_messages = [
                {"role": "system", "content": _THINK_SYSTEM_STABLE},
                {"role": "user", "content": state["query"]},
            ]

        # P0-Opt2: 动态上下文作为 "live zone" 追加 — 不修改 base_messages
        # 这部分每轮变化，不参与 KV Cache 前缀匹配
        dynamic_parts = [
            f"当前状态：迭代 {state['iteration']}/{state['max_iterations']}"
        ]
        if state["retrieved_docs"]:
            dynamic_parts.append(f"已有文档 {len(state['retrieved_docs'])} 篇")
        if state["tool_results"]:
            dynamic_parts.append(f"工具结果 {len(state['tool_results'])} 条")
        dynamic_parts.append("请决定下一步。")
        dynamic_context = "，".join(dynamic_parts)

        messages: list[Message] = list(base_messages) + [
            {"role": "user", "content": dynamic_context}
        ]

        try:
            text = ""
            async for chunk in self.llm.chat(messages, stream=False):
                if isinstance(chunk, str):
                    text += chunk
            decision = self._parse_decision(text)
            log.info(
                "engine.think",
                iteration=state["iteration"],
                decision=decision,
            )
            return decision
        except Exception as exc:
            log.error("engine.think_error", error=str(exc))
            return "generate"

    @staticmethod
    def _parse_decision(text: str) -> str:
        """解析 LLM 决策文本为路由信号。"""
        lower = text.strip().lower()
        if "retrieve" in lower:
            return "retrieve"
        if "tool_call" in lower or "tool" in lower:
            return "tool_call"
        return "generate"

    # ------------------------------------------------------------------
    # retrieve：检索 → 权限过滤 → 重排（关键：权限过滤在重排之前！）
    # ------------------------------------------------------------------

    @trace_node("retrieve")
    async def _retrieve(
        self,
        state: AgentState,
        kb_ids: list[str] | None,
    ) -> None:
        """检索知识库 — 检索 → 权限过滤 → 重排。

        ⚠️ 权限过滤必须在重排之前：先过滤掉用户无权访问的文档，
        再对剩余文档重排，确保重排结果不含越权文档，且不浪费重排算力。

        正确顺序：检索召回 → ABAC 权限过滤 → 重排 → 生成
        """
        query = state["query"]

        # 1. 多路检索召回候选
        candidates = await self.retriever.search(query, kb_ids=kb_ids, top_k=_RETRIEVE_TOP_K)
        log.info(
            "engine.retrieve.candidates",
            count=len(candidates),
            iteration=state["iteration"],
        )

        # 2. ABAC 权限过滤（必须在重排之前！）
        filtered = candidates
        if self.permission_filter is not None:
            try:
                filtered = await self.permission_filter(candidates)
                log.info(
                    "engine.retrieve.permission_filtered",
                    before=len(candidates),
                    after=len(filtered),
                )
            except Exception as exc:
                log.error("engine.retrieve.permission_error", error=str(exc))
                # 权限过滤出错时保守处理：返回空，避免泄露越权文档
                filtered = []

        # 3. 重排
        if filtered:
            try:
                reranked = await self.reranker.rerank(
                    query=query,
                    documents=filtered,
                    top_k=_RERANK_TOP_K,
                )
                # 将重排分数回填到原文档，并按重排顺序排列
                state["retrieved_docs"] = self._apply_rerank_scores(filtered, reranked)
            except Exception as exc:
                log.warning("engine.retrieve.rerank_error", error=str(exc))
                state["retrieved_docs"] = filtered[:_RERANK_TOP_K]
        else:
            state["retrieved_docs"] = []

        log.info(
            "engine.retrieve.done",
            final_count=len(state["retrieved_docs"]),
            iteration=state["iteration"],
        )

    @staticmethod
    def _apply_rerank_scores(
        docs: list[dict[str, Any]],
        reranked: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将重排结果（index+score）回填到原文档列表，按重排顺序输出。"""
        result: list[dict[str, Any]] = []
        for item in reranked:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs):
                doc = dict(docs[idx])
                doc["score"] = item.get("score", doc.get("score", 0.0))
                doc["rerank_content"] = item.get("content", doc.get("content", ""))
                result.append(doc)
        # 若重排结果不足，补上未命中的原文档
        if len(result) < len(docs):
            seen = {d.get("chunk_id") for d in result}
            for doc in docs:
                if doc.get("chunk_id") not in seen:
                    result.append(doc)
        return result

    # ------------------------------------------------------------------
    # tool_call：通过 MCP Client 调用工具
    # ------------------------------------------------------------------

    @trace_node("tool_call")
    async def _tool_call(self, state: AgentState) -> None:
        """通过 MCP Client 调用企业系统工具。

        将 MCP 工具列表传给 LLM，由 LLM 决定调用哪个工具及入参，
        再通过 MCPClient.call_tool_from_llm 转发执行。
        """
        try:
            tools = await self.mcp.get_tools_for_llm()
        except Exception as exc:
            log.warning("engine.tool_call.list_error", error=str(exc))
            return

        if not tools:
            log.info("engine.tool_call.no_tools")
            return

        messages: list[Message] = [
            {
                "role": "system",
                "content": (
                    "根据用户问题选择合适的工具调用。如无需调用工具，直接回复原文。"
                ),
            },
            {"role": "user", "content": state["query"]},
        ]

        try:
            async for chunk in self.llm.chat(messages, tools=tools, stream=False):
                if isinstance(chunk, dict) and chunk.get("type") == "tool_use":
                    await self._execute_tool_use(state, chunk)
        except Exception as exc:
            log.error("engine.tool_call.error", error=str(exc))

    async def _execute_tool_use(self, state: AgentState, tool_use: ToolUse) -> None:
        """执行单个 ToolUse — 通过 MCPClient 调用并将结果存入 state。"""
        tool_name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})
        tool_use_id = tool_use.get("id", "")
        log.info(
            "engine.tool_call.execute",
            tool=tool_name,
            tool_use_id=tool_use_id,
        )
        try:
            result = await self.mcp.call_tool(tool_name, tool_input)
            state["tool_results"].append(
                {
                    "tool": tool_name,
                    "arguments": tool_input,
                    "result": result,
                    "content": result,
                }
            )
            log.info("engine.tool_call.done", tool=tool_name, result_len=len(result))
        except Exception as exc:
            log.error("engine.tool_call.execute_error", tool=tool_name, error=str(exc))
            error_result = json.dumps(
                {"error": str(exc), "tool": tool_name},
                ensure_ascii=False,
            )
            state["tool_results"].append(
                {
                    "tool": tool_name,
                    "arguments": tool_input,
                    "result": error_result,
                    "content": error_result,
                }
            )

    # ------------------------------------------------------------------
    # reflect：自我反思
    # ------------------------------------------------------------------

    @trace_node("reflect")
    async def _reflect(self, state: AgentState) -> None:
        """自我反思 — 评估答案质量（引用 / 完整性 / 幻觉风险）。

        当前实现仅记录反思结论，不触发重试（避免在流式输出后二次循环）；
        反思日志可用于质量监控与知识缺口识别。
        """
        prompt = (
            "评估以下回答的质量：\n"
            "1. 是否有引用来源？\n"
            "2. 是否完整回答了用户问题？\n"
            "3. 是否有幻觉风险？\n"
            "简要回答（satisfied / needs_improvement）并说明原因。"
        )
        messages: list[Message] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": state["query"]},
            {"role": "assistant", "content": state.get("answer", "")},
        ]
        try:
            text = ""
            async for chunk in self.llm.chat(messages, stream=False):
                if isinstance(chunk, str):
                    text += chunk
            log.info(
                "engine.reflect",
                iteration=state["iteration"],
                conclusion=text[:200],
                session_id=state["session_id"],
            )
        except Exception as exc:
            log.warning("engine.reflect_error", error=str(exc))

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize(result: dict[str, Any] | str) -> str:
        """将工具结果摘要为决策 prompt 可读的短文本。"""
        if isinstance(result, str):
            return result[:300]
        if isinstance(result, dict):
            content = result.get("result") or result.get("content") or result
            return str(content)[:300]
        return str(result)[:300]
