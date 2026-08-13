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
    - ``answer()`` 返回 ``AsyncIterator[SSEEvent | str]`` 供 SSE 流式消费
      （SSE 事件：thinking/retrieve/tool_call/sources/quality/done；str：token）；
    - ``answer_with_graph()`` 返回 ``AsyncIterator[str]``（LangGraph 可选路径）；
    - 工具调用通过 MCPClient 转发，不耦合具体工具实现。

遵循单一职责：本模块只负责流程编排，检索/重排/生成/权限均委托注入的组件。
遵循依赖倒置：LLM、检索器、重排器、权限过滤器均通过构造注入，可替换为 Mock。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypedDict

from app.eval.span_types import SpanType
from app.llm.base import LLMProvider, Message, ToolUse
from app.mcp.client import MCPClient
from app.observability.langfuse_tracer import (
    TraceContext,
    map_span_type,
    trace_node,
)
from app.rag.cache import TokenCache
from app.rag.constitution import get_constraint_reminder, get_system_prompt
from app.rag.context_budget import ContextBudgetManager, SpillStore, _SPILL_TOOL_RESULT_THRESHOLD
from app.rag.context_dedup import CrossTurnDeduplicator
from app.rag.generator import Generator
from app.rag.tool_guard import DangerousToolGuard
from app.core.budget import HardBudget, RunBudget, check_budget, get_budget_status
from app.core.exceptions import BudgetExceeded
from app.observability.trail_aggregator import get_trail_aggregator
from app.utils.request_context import get_request_id
from app.rag.reranker import RerankerBase
from app.rag.retriever import HybridRetriever
from app.utils.logger import get_logger
from app.utils.sse import SSEEvent, SSEEventType

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

# 后台任务强引用集合 — 事件循环对 Task 仅持弱引用，不保存强引用任务可能
# 被 GC 提前回收（CPython 官方文档明确警告）；加入本集合并通过
# done_callback 自动移除，兼顾防 GC 与防泄漏。
_background_tasks: set[asyncio.Task] = set()

# P0 并发修复：引擎实例是进程级单例（factory.get_rag_engine 全局复用），
# 请求级状态（token 用量/检索重试计数/去重器/预算管理器/LangFuse 追踪上下文）
# 若保存在实例属性上，并发请求会互相覆盖 —— 用量串账、重试计数错乱、追踪串扰。
# 改用 ContextVar 隔离：每个请求任务拥有独立的状态副本。
_usage_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "rag_usage", default=None
)
_retry_count_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "rag_retry_count", default=0
)
_trace_ctx_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "rag_trace_ctx", default=None
)
_dedup_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "rag_dedup", default=None
)
_budget_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "rag_budget", default=None
)
# P1-1: 大工具结果写盘存储 —— 超大工具返回写盘，上下文只留 placeholder
_spill_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "rag_spill_store", default=None
)
# P0-3: 四轴硬预算 —— turns/seconds/tokens/cost_usd 任一触顶即硬停
_hard_budget_ctx: contextvars.ContextVar[HardBudget | None] = contextvars.ContextVar(
    "rag_hard_budget", default=None
)
_run_budget_ctx: contextvars.ContextVar[RunBudget | None] = contextvars.ContextVar(
    "rag_run_budget", default=None
)

# 默认最大迭代次数
_DEFAULT_MAX_ITERATIONS: int = 5
# 重排默认 top_k
_RERANK_TOP_K: int = 5
# 检索默认 top_k
_RETRIEVE_TOP_K: int = 20
# P1-10: 在线回退双跑评估的召回 top_k（小样本对比，控成本）
_FALLBACK_EVAL_TOP_K: int = 5

# P1-2: 宪法独立成文件 — 核心约束从 CONSTITUTION.md 读取（人机共治、前缀稳定）。
# 以下两个常量由宪法加载器派生，保持向后兼容（测试仍从本模块导入）。
#
# P0-Opt2: 稳定 system prompt — 不含动态内容（迭代计数/文档数/工具数），
# 使前缀字节稳定以命中 Anthropic KV Cache。
# 动态状态作为 "live zone" 消息在每轮 think 中追加，不嵌入 system prompt。
#
# P0-1: 尾部约束提醒（首尾三明治）— 追加到 think 上下文末尾，形成注意力尾部高地。
# 对抗 Lost in the Middle：核心约束一旦被长历史/工具返回推到上下文中间位置，
# 在注意力分布里就等于"看不见"（在 ≠ 被看见）。在末尾重放约束，保证模型在
# 生成下一个决策前注意力必扫过它——就像考试前老师最后说一句"记得写名字"。
# 设计要点：
#   - 内容稳定、不随轮次变化、不参与压缩（L0 禁区），保证可复现；
#   - 位于上下文尾部，不影响头部 KV Cache 前缀（前缀仍稳定命中缓存）。
_THINK_SYSTEM_STABLE: str = get_system_prompt()
_CONSTRAINT_REMINDER: str = get_constraint_reminder()

# 权限过滤器类型 — 接收候选文档列表，返回过滤后的列表。
# 由调用方注入（通常封装 PermissionService.filter_documents 对 dict 的适配）。
PermissionFilter = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]


def _safe_serialize(obj: Any) -> Any:
    """安全序列化 — 将可能含不可 JSON 序列化对象的嵌套结构转为纯 dict/list/str。

    P1-4: AgentState 快照持久化到 JSONB 时调用，确保 messages / retrieved_docs /
    tool_results 中可能的非标准类型（如 datetime / UUID / 自定义对象）被正确转换。
    """
    try:
        # 先尝试直接 json.dumps 验证可序列化性
        json.dumps(obj, ensure_ascii=False, default=str)
        return obj
    except (TypeError, ValueError):
        # 不可直接序列化 — 递归转换
        if isinstance(obj, dict):
            return {str(k): _safe_serialize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_safe_serialize(item) for item in obj]
        return str(obj)


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
    # P2-B: 查询重写后的查询文本（用于检索），None 表示未重写
    rewritten_query: str | None
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
    # 多租户预留 — 当前不实施隔离逻辑，仅预留字段供未来扩展。
    tenant_id: str | None
    # P3-E: Scratchpad 草稿本 — 累积每轮推理笔记，压缩时优先保留
    scratchpad: str
    # P4-E: 对话焦点传入引擎（来自 TopicTracker / DriftDetector）
    conversation_focus: dict[str, Any] | None
    # P4-E: 漂移检测结果（来自 DriftDetector）
    drift_info: dict[str, Any] | None
    # 请求级 ABAC 权限过滤器（callable，携带当前用户上下文）— 优先于
    # 引擎构造级 permission_filter；仅纯 Python answer() 路径使用。
    permission_filter: PermissionFilter | None
    # P0 wiki 层级：检索层级过滤 — series_id/path_prefix/parent_id/depth/
    # version_of。透传到 retriever.search，由 filter_builder 转为后端 filter。
    # None 表示不做层级过滤（向后兼容旧调用）。
    filters: dict[str, Any] | None
    # --- LangGraph 专用字段（纯 Python 路径不使用）---
    # think 节点产出的路由信号：retrieve / tool_call / generate。
    _decision: str
    # generate 节点产出的逐 token 片段，供 answer_with_graph 流式回放。
    _stream_tokens: list[str]
    # P1-9: 显式计划状态清单 — [{step_id, action, description, status}]
    # status: pending / done / skipped（app.agents.planner 常量）
    plan_steps: list[dict[str, Any]]
    # P1-9: 本会话已发生的重规划次数（上限由 PlanManager.max_replans 控制）
    replan_count: int


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
        # 默认路径（纯 Python while 循环）— yield SSEEvent | str
        async for chunk in engine.answer(query, user_id, session_id):
            yield chunk  # SSEEvent（thinking/retrieve/tool_call/...）或 str token

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
        tool_guard: DangerousToolGuard | None = None,
        quality_guard: Any = None,
        query_rewriter: Any = None,
        faq_matcher: Any = None,
        planner: Any = None,
        injection_guard: Any = None,
    ) -> None:
        self.llm = llm
        self.mcp = mcp_client
        self.retriever = retriever
        self.reranker = reranker
        self.generator = generator
        self.cache = cache
        self.permission_filter = permission_filter
        self.max_iterations = max_iterations
        # FAQ 快捷匹配器 — 缓存未命中后、向量检索前的零 LLM 快捷路径
        # 未注入时尝试自动初始化，失败则禁用（降级到完整 RAG 链路）
        self._faq_matcher = faq_matcher
        if self._faq_matcher is None:
            try:
                from app.rag.faq_matcher import FAQMatcher
                self._faq_matcher = FAQMatcher()
            except Exception as exc:
                log.warning("engine.faq_matcher_init_failed", error=str(exc))
                self._faq_matcher = None
        # LangGraph 断点保存器（可选）— 传入 PostgresSaver 实例后，
        # build_graph() 会将其编译进状态图，支持中断恢复。
        self._checkpointer = checkpointer
        # 编译后的状态图缓存（首次 build_graph 后复用）。
        self._compiled_graph: Any = None
        # LangFuse 追踪上下文 / 去重器 / 预算管理器 / 重试计数 / 用量累加器
        # 均为请求级状态 — 不在此处赋值，由下方 ContextVar 属性按请求上下文
        # 惰性创建，避免单例引擎在并发请求间互相污染（P0 并发修复）。
        # MCP 工具调用守卫 — 危险工具拦截器（HITL 门禁）
        # 默认实例化内置配置，也可通过构造注入自定义配置或 Mock
        self._tool_guard: DangerousToolGuard = tool_guard or DangerousToolGuard()
        # RAG 质量守卫 — 双层自适应评估闭环
        # 检索层：重排分数均值检查（零 LLM 调用）
        # 生成层：LLMJudgeService 结构化评分（复用已有 Judge）
        if quality_guard is not None:
            self._quality_guard = quality_guard
        else:
            try:
                from app.rag.quality_guard import QualityGuard
                self._quality_guard = QualityGuard()
            except Exception:
                self._quality_guard = None
        # 幻觉防护 — 矛盾检测器：检测答案与知识库文档的矛盾
        self._contradiction_detector: Any = None
        try:
            from app.context.contradiction_detector import ContradictionDetector
            self._contradiction_detector = ContradictionDetector(llm=llm)
        except Exception as exc:
            log.debug("engine.contradiction_detector_init_failed", error=str(exc))
        # 幻觉防护 — 高风险信息检测器：金额/日期/法律条款二次核验
        self._high_risk_detector: Any = None
        try:
            from app.context.high_risk_detector import HighRiskDetector
            self._high_risk_detector = HighRiskDetector()
        except Exception as exc:
            log.debug("engine.high_risk_detector_init_failed", error=str(exc))
        # P2-B: 查询重写器 — 检索前优化用户查询
        # 传入时直接使用，未传入时尝试从工厂获取（可能返回 None）
        self._query_rewriter = query_rewriter
        if self._query_rewriter is None:
            try:
                from app.rag.query_rewriter import get_query_rewriter
                self._query_rewriter = get_query_rewriter()
            except Exception as exc:
                log.warning("engine.query_rewriter_init_failed", error=str(exc))
                self._query_rewriter = None

        # P1-9: 显式计划管理器 — plan 状态清单 + 偏离检测 + 仅重规划剩余
        # 传入时直接使用（测试注入），未传入时用同一 LLM 创建
        self._planner: Any = planner
        if self._planner is None:
            try:
                from app.agents.planner import PlanManager

                self._planner = PlanManager(llm)
            except Exception as exc:
                log.warning("engine.planner_init_failed", error=str(exc))
                self._planner = None

        # Find Skills 渐进式技能加载 — 按需加载工具 schema，避免全量加载浪费 token
        # 启动时从 MCP Server 构建轻量技能索引，Agent Loop 每轮按查询匹配相关技能
        self._skill_finder: Any = None
        self._skill_registry: Any = None
        try:
            from app.config import get_settings
            from app.rag.skill_finder import SkillFinder
            from app.rag.skill_registry import SkillRegistry

            settings = get_settings()
            if getattr(settings, "SKILL_FINDER_ENABLED", True):
                self._skill_registry = SkillRegistry()
                # 延迟加载：首次 _tool_call 时从 mcp_client 构建
                self._skill_finder = SkillFinder(
                    registry=self._skill_registry,
                    match_threshold=getattr(settings, "SKILL_MATCH_THRESHOLD", 5),
                    max_skills=getattr(settings, "SKILL_MAX_LOADED", 10),
                )
        except Exception as exc:
            log.warning("engine.skill_finder_init_failed", error=str(exc))
            self._skill_finder = None
            self._skill_registry = None

        # P1-7: Agent Loop 超时分级 — 单步骤超时 / 总任务超时（读取配置，兜底默认值）
        try:
            from app.config import get_settings as _get_settings
            _s = _get_settings()
            self._step_timeout_s: float = float(
                getattr(_s, "AGENT_STEP_TIMEOUT_SECONDS", 60.0)
            )
            self._total_timeout_s: float = float(
                getattr(_s, "AGENT_TOTAL_TIMEOUT_SECONDS", 300.0)
            )
        except Exception:
            self._step_timeout_s = 60.0
            self._total_timeout_s = 300.0

        # P1-4: 检索结果注入扫描守卫 — HybridRetriever 返回后扫描 prompt injection
        # 传入时直接使用（测试注入）；未传入时从工厂获取单例（可能为 None，
        # 当 RAG_INJECTION_GUARD_ENABLED=False 时禁用）。
        # 告警回调由调用方（如 app.main lifespan）通过 get_injection_guard(alert_callback=...)
        # 预绑定到单例；engine 不主动注入回调，避免 RAG 层反向依赖 NotificationService/DB。
        self._injection_guard: Any = injection_guard
        if self._injection_guard is None:
            try:
                from app.rag.injection_guard import get_injection_guard
                self._injection_guard = get_injection_guard()
            except Exception as exc:
                log.warning("engine.injection_guard_init_failed", error=str(exc))
                self._injection_guard = None

    # ------------------------------------------------------------------
    # 请求级状态（ContextVar 隔离，并发安全）
    #
    # 引擎单例被所有请求共享，以下状态必须在请求上下文间隔离：
    # 每个请求任务首次访问时惰性创建独立实例，互不可见。
    # ------------------------------------------------------------------

    @property
    def _accumulated_usage(self) -> dict[str, Any]:
        """P0-Stage2: 用量累加器 — 累积单次 answer() 内所有 LLM 调用的 token 用量。"""
        usage = _usage_ctx.get()
        if usage is None:
            usage = {"input_tokens": 0, "output_tokens": 0, "model": ""}
            _usage_ctx.set(usage)
        return usage

    @_accumulated_usage.setter
    def _accumulated_usage(self, value: dict[str, Any]) -> None:
        _usage_ctx.set(value)

    @property
    def _retrieval_retry_count(self) -> int:
        """检索重试计数（每次 answer 调用时重置）。"""
        return _retry_count_ctx.get()

    @_retrieval_retry_count.setter
    def _retrieval_retry_count(self, value: int) -> None:
        _retry_count_ctx.set(value)

    @property
    def _trace_ctx(self) -> TraceContext | None:
        """LangFuse 追踪上下文（每次 answer 调用时重置）。"""
        return _trace_ctx_var.get()

    @_trace_ctx.setter
    def _trace_ctx(self, value: TraceContext | None) -> None:
        _trace_ctx_var.set(value)

    @property
    def _dedup(self) -> CrossTurnDeduplicator:
        """P1-Opt3: 跨轮工具结果去重器 — 检测重复结果并用指针引用替代。"""
        inst = _dedup_ctx.get()
        if inst is None:
            inst = CrossTurnDeduplicator()
            _dedup_ctx.set(inst)
        return inst

    @_dedup.setter
    def _dedup(self, value: CrossTurnDeduplicator) -> None:
        _dedup_ctx.set(value)

    @property
    def _budget(self) -> ContextBudgetManager:
        """P2-Opt6: 上下文预算管理器 — think 上下文超预算时压缩早期消息。"""
        inst = _budget_ctx.get()
        if inst is None:
            inst = ContextBudgetManager()
            _budget_ctx.set(inst)
        return inst

    @_budget.setter
    def _budget(self, value: ContextBudgetManager) -> None:
        _budget_ctx.set(value)

    @property
    def _spill_store(self) -> SpillStore:
        """P1-1: 大工具结果写盘存储 — 超大工具返回写盘，上下文只留 placeholder。"""
        inst = _spill_ctx.get()
        if inst is None:
            inst = SpillStore()
            _spill_ctx.set(inst)
        return inst

    @_spill_store.setter
    def _spill_store(self, value: SpillStore) -> None:
        _spill_ctx.set(value)

    @property
    def _hard_budget(self) -> HardBudget:
        """P0-3: 四轴硬预算配置 —— 从 Settings 读取，ContextVar 隔离。"""
        inst = _hard_budget_ctx.get()
        if inst is None:
            from app.config import get_settings as _get_settings
            _s = _get_settings()
            inst = HardBudget(
                max_turns=_s.AGENT_BUDGET_MAX_TURNS,
                max_seconds=_s.AGENT_BUDGET_MAX_SECONDS,
                max_tokens=_s.AGENT_BUDGET_MAX_TOKENS,
                max_cost_usd=_s.AGENT_BUDGET_MAX_COST_USD,
            )
            _hard_budget_ctx.set(inst)
        return inst

    @property
    def _run_budget(self) -> RunBudget:
        """P0-3: 运行时累积预算 —— 每次 LLM 调用后更新，决策循环每轮检查。"""
        inst = _run_budget_ctx.get()
        if inst is None:
            inst = RunBudget()
            _run_budget_ctx.set(inst)
        return inst

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
        tenant_id: str | None = None,
        db: Any = None,
        user_uuid: uuid.UUID | None = None,
        conversation_focus: dict[str, Any] | None = None,
        drift_info: dict[str, Any] | None = None,
        permission_filter: PermissionFilter | None = None,
        cache_scope: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> AsyncIterator[SSEEvent | str]:
        """Agentic RAG 主入口 — 返回 SSE 事件流供前端实时消费。

        P0-2 重构：从仅生成阶段 yield token 升级为全流程 yield SSE 事件，
        让用户在 think / retrieve / tool_call 阶段就能看到实时进度。

        P1-4 更新：新增 ``db`` / ``user_uuid`` 参数，当 DangerousToolGuard
        拦截危险工具时，通过 ApprovalService 创建审批记录并 yield
        ``approval_required`` SSE 事件，支持前端弹窗审批 + 服务重启恢复。

        事件流时序::

            event: thinking         ← think 阶段（每轮）
            event: retrieve_start   ← 检索开始
            event: retrieve_end     ← 检索完成（含文档数）
            event: tool_call_start  ← 工具调用开始（P0-3）
            event: tool_call_end    ← 工具调用完成（P0-3）
            data: 根据…             ← token（generate 阶段，plain str）
            event: sources          ← 引用来源
            event: quality          ← 质量评分
            event: done             ← 结束信号（含 token_count / iterations）

        Args:
            query: 用户问题。
            user_id: 当前用户 ID（用于权限过滤）。
            session_id: 会话 ID。
            kb_ids: 可选，限定检索的知识库范围。显式传空列表表示用户
                无任何可访问知识库，检索短路返回空结果（不会回落为全库检索）。
            memory_context: 记忆引擎提供的上下文。
            tenant_id: 租户 ID（答案缓存按租户隔离，防止跨租户答案泄漏）。
            permission_filter: 可选，请求级 ABAC 权限过滤器（携带当前用户
                上下文）。引擎为全局单例，构造级 ``self.permission_filter``
                无法区分用户；请求级参数优先于构造级注入。
            cache_scope: 可选，权限视图指纹（由调用方按可访问 kb_ids +
                用户密级计算）。参与答案缓存 key 计算，不同权限视图的
                用户互不可见，防止缓存跨权限泄漏。
            filters: P0 wiki 层级过滤 — series_id/path_prefix/parent_id/
                depth/version_of。透传到 retriever.search 限定检索范围
                （如"只在某系列/子 wiki 下检索"）。filters 指纹自动并入
                cache_scope，防止不同层级视图的用户互读缓存（如
                series_id=X 的答案不应命中无 filters 的查询）。

        Yields:
            SSEEvent | str: SSE 事件对象（thinking/retrieve/tool_call/sources/
            quality/done）或 token 字符串（generate 阶段）。
        """
        # 无任何可访问知识库时短路：跳过 FAQ 与检索，避免空 kb_ids
        # 在底层被当作"不过滤"而回落为全库检索（跨知识库泄漏）。
        no_kb_access = kb_ids is not None and len(kb_ids) == 0
        # P0 wiki 层级：filters 指纹并入 cache_scope，防止不同层级视图
        # 缓存互读（如 series_id=X 的答案不应命中无 filters 的查询）。
        # filters 为 None 时 cache_scope 不变（向后兼容旧调用）。
        if filters:
            import hashlib

            _filters_hash = hashlib.sha256(
                json.dumps(filters, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:16]
            cache_scope = (
                f"{cache_scope}|hier={_filters_hash}"
                if cache_scope
                else f"hier={_filters_hash}"
            )
        # 1. 缓存命中检查（缓存 key 含 tenant_id + 权限视图 scope，
        # 跨租户 / 跨权限视图互不可见）。
        # 无可访问知识库（no_kb_access）时跳过缓存读取：缓存中的答案
        # 可能来自有权限用户的权限视图，直接命中会造成越权泄漏。
        if self.cache is not None and not no_kb_access:
            try:
                cached = await self.cache.get(
                    query, tenant_id=tenant_id, scope=cache_scope
                )
                if cached is not None:
                    log.info("engine.cache.hit", session_id=session_id)
                    # P1-3: 缓存命中短路路径补最小 Trace（此前完全无追踪记录，
                    # 评测无法观测缓存命中路径）
                    self._record_shortcut_trace(
                        name="cache_hit",
                        span_type="cache.lookup",
                        query=query,
                        user_id=user_id,
                        session_id=session_id,
                        output=cached[:200],
                        metadata={"source": "answer_cache"},
                    )
                    yield cached
                    return
            except Exception as exc:
                log.warning("engine.cache.get_error", error=str(exc))

        # 1.5 FAQ 快捷匹配 — 缓存未命中后，尝试 BM25 精准匹配 faq chunk
        # 命中时直接返回答案，跳过 Agent Loop（零 LLM 调用）
        if self._faq_matcher is not None and not no_kb_access:
            try:
                # 局部导入 — 与 __init__ 中 FAQMatcher 的延迟导入策略一致，
                # faq_matcher 依赖不可用时引擎整体仍可用（_faq_matcher 为 None
                # 时本分支不会进入）。
                from app.rag.faq_matcher import FAQMatchResult

                faq_result = await self._faq_matcher.match(query, kb_ids=kb_ids)
                if faq_result.matched:
                    # Bug3 修复：FAQ 检索仅按 kb_ids 做了知识库归属下推，
                    # 未做文档密级检查 — 低密级用户可直接读到高密级文档的
                    # FAQ 答案。返回答案前对命中文档执行请求级权限过滤
                    # （与 _retrieve 同一约束：请求级优先于构造级），
                    # 被过滤则放弃 FAQ 短路、降级到正常检索路径。
                    faq_allowed = True
                    active_filter = permission_filter or self.permission_filter
                    if active_filter is not None:
                        try:
                            passed = await active_filter(
                                [
                                    {
                                        "doc_id": faq_result.doc_id,
                                        "kb_id": faq_result.kb_id,
                                        "chunk_id": faq_result.chunk_id,
                                    }
                                ]
                            )
                            faq_allowed = bool(passed)
                        except Exception as exc:
                            # 权限过滤异常 — fail-closed，放弃短路
                            log.warning("engine.faq.permission_error", error=str(exc))
                            faq_allowed = False
                    if not faq_allowed:
                        log.warning(
                            "engine.faq.permission_denied",
                            session_id=session_id,
                            doc_id=faq_result.doc_id,
                        )
                        faq_result = FAQMatchResult(matched=False)
                if faq_result.matched:
                    log.info(
                        "engine.faq.hit",
                        session_id=session_id,
                        score=faq_result.score,
                        chunk_id=faq_result.chunk_id,
                    )
                    # 写入缓存（与完整答案共享缓存层，key 含权限视图 scope）
                    if self.cache is not None:
                        try:
                            # 携带 doc_ids：FAQ 源文档更新时可主动失效本缓存，
                            # 否则旧 FAQ 答案在文档修订后仍被缓存复用。
                            _faq_doc_ids = (
                                [faq_result.doc_id] if faq_result.doc_id else None
                            )
                            await self.cache.set(
                                query,
                                faq_result.answer,
                                tenant_id=tenant_id,
                                doc_ids=_faq_doc_ids,
                                scope=cache_scope,
                            )
                        except Exception:
                            pass
                    # P1-3: FAQ 命中短路路径补最小 Trace
                    self._record_shortcut_trace(
                        name="faq_hit",
                        span_type="faq.match",
                        query=query,
                        user_id=user_id,
                        session_id=session_id,
                        output=faq_result.answer[:200],
                        metadata={
                            "source": "faq",
                            "score": faq_result.score,
                            "chunk_id": faq_result.chunk_id,
                        },
                    )
                    yield faq_result.answer
                    return
            except Exception as exc:
                log.warning("engine.faq.match_error", error=str(exc))

        # 2. 初始化状态
        state: AgentState = {
            "query": query,
            "rewritten_query": None,
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
            "tenant_id": tenant_id,
            "conversation_focus": conversation_focus,
            "drift_info": drift_info,
            # 请求级权限过滤器（携带用户上下文），优先于构造级注入
            "permission_filter": permission_filter,
            # P0 wiki 层级：检索层级过滤（透传到 _retrieve → retriever.search）
            "filters": filters,
            # P1-4: 知识库版本快照 — 记录本次查询使用的 KB 版本时间戳，
            # 写入 _span_evidence 供 trace 回放追溯"哪一版资料造成了这个答案"。
            "kb_version_snapshot": datetime.now(timezone.utc).isoformat(),
        }
        # 重置检索重试计数
        self._retrieval_retry_count = 0
        # P0-Stage2: 重置用量累加器
        self._accumulated_usage = {"input_tokens": 0, "output_tokens": 0, "model": ""}

        # P1-Opt3: 每次新对话重置去重器（清空上一轮对话的已见列表）
        self._dedup.reset()
        # P2-Opt6: 重置预算管理器统计
        self._budget.reset()
        # P0-3: 重置四轴硬预算累积器（新一轮 answer 从零开始计数）
        self._run_budget.reset()
        self._run_budget.run_id = session_id

        # 2.5 初始化 LangFuse 追踪
        # P0-Stage3: 关联 HTTP request_id，使 LangFuse 追踪可按请求 ID 搜索
        _http_request_id = get_request_id()
        # P0-3: 生产路径工具审计 — 非评测模式（无激活 recorder）且 db 可用时，
        # 为本次 run 创建本地 SpanRecorder 并显式注入 TraceContext，
        # 使 tool.call 审计 Span 可收集并在 answer() 尾部落库 tool_audit_log。
        from app.observability.span_record import (
            SpanRecorder,
            get_current_recorder,
        )

        _audit_recorder: SpanRecorder | None = None
        if db is not None and get_current_recorder() is None:
            _audit_recorder = SpanRecorder()
        self._trace_ctx = TraceContext(
            trace_name="rag_agent_loop",
            session_id=session_id,
            user_id=user_id,
            metadata={
                "query": query[:200],
                "http_request_id": _http_request_id,
            },
            recorder=_audit_recorder,
        )
        self._trace_ctx.start()

        # 2.8 P2-B: 查询重写 — 检索前优化用户查询
        if self._query_rewriter is not None:
            try:
                # P1-10: 在线回退 — 开关开启时注入召回评估器（小 top_k 双跑对比，
                # 判定结果随 rewriter LRU 缓存复用，同一查询只双跑一次）。
                # 用 `is True` 判型且仅在启用时传参：测试注入的 Mock/Fake rewriter
                # 无此属性，保持其 rewrite(query, context) 签名兼容。
                if getattr(self._query_rewriter, "enable_online_fallback", False) is True:
                    rewrite_result = await self._query_rewriter.rewrite(
                        query=query,
                        context=memory_context,
                        recall_evaluator=self._build_recall_evaluator(kb_ids),
                    )
                else:
                    rewrite_result = await self._query_rewriter.rewrite(
                        query=query,
                        context=memory_context,
                    )
                rewritten = rewrite_result.get_search_query()
                if rewritten and rewritten != query:
                    state["rewritten_query"] = rewritten
                    log.info(
                        "engine.query_rewritten",
                        original=query[:100],
                        rewritten=rewritten[:100],
                        strategy=rewrite_result.strategy,
                        latency_ms=rewrite_result.latency_ms,
                    )
                # 发送 query_rewrite SSE 事件 — 让前端展示重写过程
                yield SSEEvent(
                    data=rewrite_result.to_dict(),
                    event=SSEEventType.QUERY_REWRITE,
                )
            except Exception as exc:
                log.warning(
                    "engine.query_rewrite_failed",
                    error=str(exc),
                    query=query[:100],
                )

        # 3. 执行 think / retrieve / tool_call 循环 — 流式 yield SSE 事件
        # P1-4: 传入 db / user_uuid 以支持审批记录持久化
        async for event in self._run_decision_loop_streaming(
            state, db=db, user_uuid=user_uuid
        ):
            yield event

        # P0-3: 轨迹聚合埋点 — 决策循环结束后上报会话快照（失败不影响主链路）
        try:
            _tool_calls = [
                {
                    "name": r.get("tool", "unknown"),
                    "hit": "error" not in r,
                }
                for r in state.get("tool_results", [])
                if isinstance(r, dict)
            ]
            await get_trail_aggregator().record_session(
                iterations=state.get("iteration", 0),
                max_iter_reached=bool(state.get("_max_iter_hit")),
                total_timeout=bool(state.get("_total_timeout")),
                dedup_hit=state.get("_dedup_hits", 0) > 0,
                tool_calls=_tool_calls,
            )
        except Exception as exc:
            log.debug("engine.trail_record_failed", error=str(exc))

        # 4. 流式生成答案（plain str token，_to_sse_stream 自动包装为 data:）
        answer_parts: list[str] = []
        _gen_t0 = time.perf_counter()
        async for token in self.generator.generate(
            query=state["query"],
            retrieved_docs=state["retrieved_docs"],
            tool_results=state["tool_results"],
            memory_context=state.get("memory_context", ""),
        ):
            answer_parts.append(token)
            yield token
        # 记录 generate 节点 Span（trace_node 装饰器无法作用于内联流式调用）
        # P0-2: metadata 携带 included_refs — 生成阶段实际使用的上下文引用，
        # 与 retrieve span 证据互证（P0-1: 使用标准 SpanType）。
        if self._trace_ctx is not None:
            _gen_included = [
                str(d.get("doc_id") or d.get("chunk_id"))
                for d in state["retrieved_docs"]
                if isinstance(d, dict) and (d.get("doc_id") or d.get("chunk_id"))
            ]
            self._trace_ctx.span(
                name=f"generate_iter{state['iteration']}",
                span_type=SpanType.STATE_UPDATE.value,
                input_data={"query": state["query"][:200], "doc_count": len(state["retrieved_docs"])},
                output_data={"answer_preview": "".join(answer_parts)[:200]},
                metadata={
                    "latency_ms": round((time.perf_counter() - _gen_t0) * 1000, 2),
                    "token_count": len("".join(answer_parts)) // 4,
                    "included_refs": _gen_included,
                    "trust_levels": {rid: "internal" for rid in _gen_included},
                },
            )

        # P0-Stage2: 累加 generate 用量到引擎用量记录
        _gen_usage = getattr(self.generator, "last_usage", None)
        if _gen_usage:
            self._accumulated_usage["input_tokens"] += _gen_usage.get("input_tokens", 0)
            self._accumulated_usage["output_tokens"] += _gen_usage.get("output_tokens", 0)
            if _gen_usage.get("model"):
                self._accumulated_usage["model"] = _gen_usage["model"]
            # P0-3: 同步到四轴硬预算累积器（tokens + cost 轴）
            _gen_in = _gen_usage.get("input_tokens", 0)
            _gen_out = _gen_usage.get("output_tokens", 0)
            _gen_cost_usd = self._estimate_cost_cents(
                _gen_usage.get("model", ""),
                _gen_in,
                _gen_out,
            ) / 100.0  # 分 → 美元
            self._run_budget.add_usage(
                input_tokens=_gen_in,
                output_tokens=_gen_out,
                cost_usd=_gen_cost_usd,
            )

        # P0-Stage2: 写入 UsageRecord（真实 token 用量 + 真实耗时）
        if db is not None and user_uuid is not None:
            try:
                await self._write_usage_record(
                    db=db,
                    user_uuid=user_uuid,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    gen_latency_ms=round((time.perf_counter() - _gen_t0) * 1000, 2),
                )
            except Exception as exc:
                log.warning("engine.usage_record_failed", error=str(exc))

        # 5. 反思（非流式，返回结构化评测结果）
        answer = "".join(answer_parts)
        state["answer"] = answer
        eval_result = await self._reflect(state)

        # 5.5 忠实度拦截重生成答案处理：
        # _reflect 内部 check_and_regenerate 可能产出新答案（state["answer"]），
        # 但此前流式推给客户端的是旧答案。此处通过 ANSWER_REGENERATED 事件
        # 把完整新答案推送给客户端（前端替换展示），并同步后续缓存与
        # 持久化均使用新答案，避免"客户端看旧答案、缓存存旧答案"。
        if state.get("answer_regenerated") and state["answer"] != answer:
            answer = state["answer"]
            yield SSEEvent(
                data={"answer": answer},
                event=SSEEventType.ANSWER_REGENERATED,
            )

        # 6. yield sources 事件（引用来源）
        if state["retrieved_docs"]:
            sources = [
                {
                    "id": doc.get("chunk_id", ""),
                    "title": doc.get("title", doc.get("doc_title", "")),
                    "content": doc.get("content", "")[:200],
                    "score": doc.get("score", 0.0),
                }
                for doc in state["retrieved_docs"]
            ]
            yield SSEEvent(
                data={"sources": sources},
                event=SSEEventType.SOURCES,
            )

        # 7. yield quality 事件（质量评分 + 低置信度标记 + 幻觉防护结果）
        quality_data: dict[str, Any] = {
            "low_confidence": state.get("low_confidence", False),
        }
        if eval_result is not None:
            quality_data.update(
                {
                    "citation_accuracy": eval_result.citation_accuracy,
                    "completeness": eval_result.completeness,
                    "faithfulness": eval_result.hallucination_inverse,
                    "total_score": eval_result.total_score,
                }
            )
        # 幻觉防护结果上报
        if state.get("answer_regenerated"):
            quality_data["answer_regenerated"] = True
        if state.get("citation_invalid"):
            quality_data["citation_invalid"] = True
        if state.get("contradiction_blocked"):
            quality_data["contradiction_blocked"] = True
        if state.get("high_risk_blocked"):
            quality_data["high_risk_blocked"] = True
        if state.get("contradiction_result"):
            quality_data["contradiction"] = state["contradiction_result"]
        if state.get("high_risk_result"):
            quality_data["high_risk"] = state["high_risk_result"]
        if state.get("citation_validation"):
            quality_data["citation_check"] = state["citation_validation"]
        if state.get("low_confidence"):
            quality_data["message"] = "本次回答的置信度较低，建议核实关键信息"
        yield SSEEvent(
            data=quality_data,
            event=SSEEventType.QUALITY,
        )

        # 8. 回写缓存（key 含 tenant_id + 权限视图 scope，跨租户 /
        # 跨权限视图互不可见）
        # 注意：answer 此时已是重生成后的新答案（若发生过重生成）。
        # 低置信 / 被拦截（矛盾 block / 高风险 block）的答案不写入缓存，
        # 防止低质量答案被后续请求直接复用。
        # 无可访问知识库（no_kb_access）时同样不写缓存：短路产生的
        # "无内容"答案不应被缓存复用（与缓存读取的短路策略一致）。
        _cacheable = answer and not no_kb_access and not (
            state.get("low_confidence")
            or state.get("contradiction_blocked")
            or state.get("high_risk_blocked")
        )
        if self.cache is not None and _cacheable:
            try:
                # P1: 提取引用文档 ID，用于文档更新时主动失效缓存
                doc_ids = list({
                    str(d.get("doc_id")) for d in state.get("retrieved_docs", [])
                    if d.get("doc_id")
                }) or None
                await self.cache.set(
                    query, answer, tenant_id=tenant_id, doc_ids=doc_ids,
                    scope=cache_scope,
                )
            except Exception as exc:
                log.warning("engine.cache.set_error", error=str(exc))
        elif self.cache is not None and answer:
            log.info(
                "engine.cache.skip_low_quality",
                session_id=session_id,
                low_confidence=bool(state.get("low_confidence")),
                contradiction_blocked=bool(state.get("contradiction_blocked")),
                high_risk_blocked=bool(state.get("high_risk_blocked")),
            )

        # 9. 结束 Trace（含质量评分上报）
        if self._trace_ctx is not None:
            budget_stats = self._budget.get_stats()
            _real_total_tokens = (
                self._accumulated_usage["input_tokens"]
                + self._accumulated_usage["output_tokens"]
            )
            trace_metadata = {
                "iterations": state["iteration"],
                "total_tokens": _real_total_tokens if _real_total_tokens > 0 else len(answer) // 4,
                "retrieved_docs": len(state["retrieved_docs"]),
                "tool_results": len(state["tool_results"]),
                "budget_compress_count": budget_stats["compress_count"],
                "budget_tokens_saved": budget_stats["total_tokens_saved"],
                "retrieval_retry_count": self._retrieval_retry_count,
            }
            # 质量评分上报到 LangFuse
            if eval_result is not None:
                trace_metadata["quality"] = {
                    "citation_accuracy": eval_result.citation_accuracy,
                    "completeness": eval_result.completeness,
                    "faithfulness": eval_result.hallucination_inverse,
                    "total_score": eval_result.total_score,
                    "low_confidence": state.get("low_confidence", False),
                    "passed": eval_result.passed,
                }
            self._trace_ctx.finalize(
                output=answer[:500],
                metadata=trace_metadata,
            )

        # 9.5 P0-3: 生产路径工具审计落库（best-effort，失败不影响主链路）—
        # 收集本次 run 的 tool.call / permission.decision Span 写入 tool_audit_log，
        # 使安全审计与评测回溯在生产环境有数据（此前 persist_tool_spans 无调用方）。
        if _audit_recorder is not None and db is not None:
            try:
                from app.models.tool_audit import persist_tool_spans

                _audit_spans = _audit_recorder.collect()
                _written = await persist_tool_spans(
                    _audit_spans,
                    db,
                    run_id=_http_request_id or session_id,
                    session_id=session_id,
                )
                if _written:
                    await db.commit()
                    log.info(
                        "engine.tool_audit_persisted",
                        count=_written,
                        session_id=session_id,
                    )
            except Exception as exc:
                log.warning("engine.tool_audit_persist_failed", error=str(exc))

        # 10. yield done 事件（结束信号 + 统计摘要）
        _done_total_tokens = (
            self._accumulated_usage["input_tokens"]
            + self._accumulated_usage["output_tokens"]
        )
        yield SSEEvent(
            data={
                "message_id": session_id,
                "token_count": _done_total_tokens if _done_total_tokens > 0 else len(answer) // 4,
                "iterations": state["iteration"],
                "retrieved_docs": len(state["retrieved_docs"]),
                "tool_calls": len(state["tool_results"]),
            },
            event=SSEEventType.DONE,
        )

    # ------------------------------------------------------------------
    # P0-Stage2: 用量记录 — 真实 token 用量 + 成本估算 + 持久化
    # ------------------------------------------------------------------

    # 模型定价表（USD per 1M tokens）— 用于成本估算，可按需更新
    _MODEL_PRICING: dict[str, tuple[float, float]] = {
        "claude-sonnet": (3.0, 15.0),
        "claude-opus": (15.0, 75.0),
        "claude-haiku": (0.25, 1.25),
        "qwen": (0.0, 0.0),       # 免费额度或自托管
        "llama": (0.0, 0.0),       # 自托管
        "gpt-4o": (2.5, 10.0),
        "text-embedding": (0.02, 0.0),
    }

    @classmethod
    def _estimate_cost_cents(
        cls, model: str, input_tokens: int, output_tokens: int
    ) -> int:
        """根据模型定价估算成本（单位：分）。

        Args:
            model: 模型名称（如 claude-sonnet-4-6-20260217）。
            input_tokens: 输入 token 数。
            output_tokens: 输出 token 数。

        Returns:
            成本（分），自托管模型返回 0。
        """
        if not model or input_tokens + output_tokens == 0:
            return 0
        # 按前缀匹配定价
        for prefix, (in_price, out_price) in cls._MODEL_PRICING.items():
            if model.lower().startswith(prefix):
                cost_usd = (
                    input_tokens / 1_000_000 * in_price
                    + output_tokens / 1_000_000 * out_price
                )
                return int(cost_usd * 100)  # 转为分
        return 0  # 未知模型不计费

    async def _write_usage_record(
        self,
        db: Any,
        user_uuid: uuid.UUID,
        tenant_id: str | None,
        session_id: str,
        gen_latency_ms: float,
    ) -> None:
        """将累积的 token 用量写入 UsageRecord 表。

        P0-Stage2: 替代 reports.py 中硬编码的估算值，
        使使用量报表和成本报表基于真实数据。

        Args:
            db: 异步数据库会话。
            user_uuid: 用户 UUID。
            tenant_id: 租户 ID（字符串形式）。
            session_id: 会话 ID（作为 request_id 关联追踪）。
            gen_latency_ms: 生成阶段耗时（毫秒）。
        """
        from app.models.billing import UsageRecord

        usage = self._accumulated_usage
        total_tokens = usage["input_tokens"] + usage["output_tokens"]
        if total_tokens == 0:
            return  # 无用量数据则不写入

        model = usage.get("model", "")
        cost_cents = self._estimate_cost_cents(
            model, usage["input_tokens"], usage["output_tokens"]
        )

        # 解析 tenant_id
        tenant_uuid: uuid.UUID | None = None
        if tenant_id:
            try:
                tenant_uuid = uuid.UUID(tenant_id)
            except (ValueError, TypeError):
                pass

        # P0-Stage3: 优先使用 HTTP request_id 关联追踪，回退到 session_id
        _http_request_id = get_request_id()
        record = UsageRecord(
            tenant_id=tenant_uuid,
            user_id=user_uuid,
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_cents=cost_cents,
            request_type="chat",
            duration_ms=int(gen_latency_ms),
            success=True,
            request_id=_http_request_id or session_id,
        )
        db.add(record)
        await db.commit()
        log.info(
            "engine.usage_recorded",
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_cents=cost_cents,
            duration_ms=int(gen_latency_ms),
            session_id=session_id,
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
        tenant_id: str | None = None,
        cache_scope: str | None = None,
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
            tenant_id: 租户 ID（答案缓存按租户隔离，防止跨租户答案泄漏）。
            cache_scope: 可选，权限视图指纹 — 参与答案缓存 key 计算，
                不同权限视图的用户互不可见（与 answer() 语义一致）。

        Yields:
            str: 答案文本片段。
        """
        if not LANGGRAPH_AVAILABLE:
            raise RuntimeError(
                "LangGraph not installed — 请回退到默认的 answer() 方法。"
            )

        # 无可访问知识库时跳过缓存读取（与 answer() 行为一致）
        no_kb_access = kb_ids is not None and len(kb_ids) == 0
        # 1. 缓存命中检查（与 answer() 行为一致，key 含 tenant_id + scope）
        if self.cache is not None and not no_kb_access:
            try:
                cached = await self.cache.get(
                    query, tenant_id=tenant_id, scope=cache_scope
                )
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
        # 捕获 retrieve 节点产出的文档列表（用于缓存 doc_ids 失效关联）
        graph_retrieved_docs: list[dict[str, Any]] = []

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
                # retrieve 节点完成后记录候选文档（后序节点可能继续补充）
                docs_update = update.get("retrieved_docs")
                if docs_update:
                    graph_retrieved_docs = list(docs_update)
                # generate 节点完成后，逐 token 回放给消费者
                tokens = update.get("_stream_tokens")
                if tokens:
                    for token in tokens:
                        answer_parts.append(token)
                        yield token
        except Exception as exc:
            log.error("engine.graph.stream_error", error=str(exc))
            if answer_parts:
                # 已有部分答案流出 — 无法撤回，直接结束（不写缓存/不持久化错误）
                return
            # 无任何答案产出 — 原样抛出。错误文本不得作为答案 yield，
            # 否则消费方会把它当 assistant 消息持久化 / 写入缓存，
            # 且向用户泄漏内部错误细节（与 Generator.generate 的契约一致：
            # 失败时抛出而非产出，上层决定降级策略）。
            raise

        # 4. 回写缓存（key 含 tenant_id + 权限视图 scope，
        # 跨租户 / 跨权限视图互不可见）
        # 修复：优先使用图最终状态中的 answer — reflect 节点若触发
        # 忠实度重生成，最终 answer 与流式回放token拼接的旧答案不同，
        # 缓存必须写入新答案；同时携带 doc_ids 支持文档更新主动失效。
        answer = "".join(answer_parts)
        final_values: dict[str, Any] = {}
        try:
            final_snapshot = await compiled.aget_state(config)
            final_values = getattr(final_snapshot, "values", None) or {}
            final_answer = final_values.get("answer")
            if isinstance(final_answer, str) and final_answer:
                answer = final_answer
        except Exception:
            pass  # 无法读取最终状态时回退到流式拼接答案
        # 质量门禁（与 answer() 主链路一致）：低置信 / 被拦截
        # （矛盾 block / 高风险 block）的答案不写缓存，防止低质量答案被复用。
        # 无可访问知识库（no_kb_access）时同样不写缓存。
        # 标记位由 reflect 节点写入图最终状态。
        _cacheable = answer and not no_kb_access and not (
            final_values.get("low_confidence")
            or final_values.get("contradiction_blocked")
            or final_values.get("high_risk_blocked")
        )
        if self.cache is not None and _cacheable:
            try:
                doc_ids = list({
                    str(d.get("doc_id"))
                    for d in graph_retrieved_docs
                    if d.get("doc_id")
                }) or None
                await self.cache.set(
                    query, answer, tenant_id=tenant_id, doc_ids=doc_ids,
                    scope=cache_scope,
                )
            except Exception as exc:
                log.warning("engine.cache.set_error", error=str(exc))
        elif self.cache is not None and answer:
            log.info(
                "engine.graph.cache.skip_low_quality",
                session_id=session_id,
                low_confidence=bool(final_values.get("low_confidence")),
                contradiction_blocked=bool(final_values.get("contradiction_blocked")),
                high_risk_blocked=bool(final_values.get("high_risk_blocked")),
            )

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
        """执行 think → retrieve/tool_call 的循环（非流式版本，向后兼容）。

        委托给 ``_run_decision_loop_streaming`` 并排空事件流，
        供 ``answer_with_graph`` 及测试用例调用。

        历史文档参考：
            P0-Opt2: Live-Zone 增量上下文传递 — 循环开始前初始化稳定前缀
            [system_stable, user_query]，后续每轮只追加增量结果（短摘要），
            不重建完整 messages 列表。前缀字节稳定以命中 Anthropic KV Cache。

            P2-Opt6: 上下文预算保护 — 每轮 think 前检查 messages 总 token 数，
            超过预算（2000 tok）时压缩早期中间消息为单条摘要。
        """
        async for _ in self._run_decision_loop_streaming(state):
            pass

    async def _run_decision_loop_streaming(
        self,
        state: AgentState,
        db: Any = None,
        user_uuid: uuid.UUID | None = None,
    ) -> AsyncIterator[SSEEvent]:
        """执行 think → retrieve/tool_call 循环 — 流式 yield SSE 事件。

        P0-2 核心：在 think / retrieve / tool_call 阶段向客户端推送实时进度事件，
        让用户在等待生成时看到 Agent 正在做什么。

        P1-4 更新：接收 ``db`` / ``user_uuid`` 参数并透传给 ``_tool_call_streaming``，
        以支持危险工具审批记录持久化。

        事件流：
            - thinking：每轮 think 开始时
            - retrieve_start / retrieve_end：检索前后
            - tool_call_start / tool_call_end：每个工具调用前后（P0-3）
            - approval_required：危险工具需要用户确认时（P1-4）

        逻辑与原 ``_run_decision_loop`` 完全等价，仅增加了 SSE 事件 yield。
        """
        kb_ids = state.get("kb_ids")
        _loop_t0 = time.monotonic()

        # P0-Opt2: 初始化稳定前缀 — system prompt（无动态内容）+ user query
        # 此前缀在整个循环中不修改，保证 Anthropic Prompt Cache 命中。
        state["messages"] = [
            {"role": "system", "content": _THINK_SYSTEM_STABLE},
            {"role": "user", "content": state["query"]},
        ]

        # P1-9: 生成初始计划状态清单（失败降级为默认两步计划，不阻塞循环）
        if self._planner is not None and "plan_steps" not in state:
            try:
                state["plan_steps"] = await self._planner.build_initial_plan(
                    state["query"]
                )
                state["replan_count"] = 0
            except Exception as exc:
                log.warning("engine.plan_init_failed", error=str(exc))
                state["plan_steps"] = []

        # P1-9: 同一决策连续重复计数（偏离检测启发式触发器之一）
        _last_decision: str | None = None
        _same_decision_count: int = 0

        while True:
            # P0-3: 四轴硬预算前置闸门 —— turns/seconds/tokens/cost_usd 任一触顶即硬停。
            # 与下方 _total_timeout_s / max_iterations 软终止并存：
            #   - 软终止: break 循环，走兜底生成（用户仍能拿到回答）
            #   - 硬预算: 抛 BudgetExceeded 不可重试异常（拒绝继续，保护成本）
            self._run_budget.increment_turn()
            try:
                check_budget(
                    self._run_budget,
                    self._hard_budget,
                    run_id=state.get("session_id"),
                )
            except BudgetExceeded as exc:
                state["_budget_exceeded"] = True
                log.warning(
                    "engine.budget_exceeded",
                    alert=True,
                    alert_type="agent_budget_exceeded",
                    **exc.context,
                )
                # 预算耗尽走兜底生成路径（不抛异常给用户，而是用已有信息生成回答）
                break

            # P1-7: 总任务超时兜底 — 决策循环整体卡死防护（卡死监控告警）
            _elapsed = time.monotonic() - _loop_t0
            if _elapsed > self._total_timeout_s:
                state["_total_timeout"] = True
                log.warning(
                    "engine.total_timeout",
                    alert=True,
                    alert_type="agent_loop_total_timeout",
                    elapsed_s=round(_elapsed, 1),
                    iteration=state["iteration"],
                    session_id=state["session_id"],
                )
                break

            state["iteration"] += 1
            if state["iteration"] > state["max_iterations"]:
                state["_max_iter_hit"] = True
                log.warning(
                    "engine.max_iterations_reached",
                    alert=True,
                    alert_type="agent_loop_max_iterations",
                    iteration=state["iteration"],
                    session_id=state["session_id"],
                )
                break

            # yield thinking 事件 — 让用户看到 Agent 正在分析
            yield SSEEvent(
                data={
                    "content": f"正在分析问题（第 {state['iteration']} 轮）...",
                    "iteration": state["iteration"],
                },
                event=SSEEventType.THINKING,
            )

            # think：LLM 决策下一步（读取 state["messages"] 稳定前缀 + 增量结果）
            # P2-Opt6: think 前检查上下文预算，超限时压缩早期消息
            if self._budget.should_compress(state["messages"]):
                state["messages"] = self._budget.compress(
                    state["messages"],
                    scratchpad=state.get("scratchpad", ""),
                )
                # P0-2: 记录 compaction_event 证据 span（压缩原因 / 保留摘要 /
                # 丢弃内容类型 / token 前后对比），供 ContextTraceRecord 聚合
                # P2-8: 透传 state 以写入 preserved_refs（压缩后仍保留的文档引用）
                self._record_compaction_evidence(state)

            decision = await self._think_with_timeout(state)
            if decision is None:
                # 单步骤超时 — 跳出循环进入生成兜底（P1-7 卡死防护）
                break

            # P1-9: 同一决策连续重复 → 偏离检测触发器（Agent 原地决策无进展）
            if decision == _last_decision and decision in ("retrieve", "tool_call"):
                _same_decision_count += 1
            else:
                _same_decision_count = 0
            _last_decision = decision

            if decision == "retrieve":
                # yield retrieve_start 事件
                yield SSEEvent(
                    data={
                        "query": state["query"],
                        "iteration": state["iteration"],
                    },
                    event=SSEEventType.RETRIEVE_START,
                )

                await self._retrieve(state, kb_ids)

                # yield retrieve_end 事件
                yield SSEEvent(
                    data={
                        "doc_count": len(state["retrieved_docs"]),
                        "iteration": state["iteration"],
                    },
                    event=SSEEventType.RETRIEVE_END,
                )

                # P0-Opt2: 追加增量结果摘要（非重建），前缀保持稳定
                state["messages"].append(
                    {
                        "role": "user",
                        "content": f"[系统] 已检索到 {len(state['retrieved_docs'])} 篇文档",
                    }
                )
                # P3-E: Scratchpad 追加推理笔记
                _sp = state.get("scratchpad", "")
                state["scratchpad"] = _sp + f"\n[轮{state['iteration']}] retrieve: 检索到 {len(state['retrieved_docs'])} 篇文档"

                # P1-9: 推进计划状态 + 偏离检测（触发器：检索为空 / 决策重复）
                if state.get("plan_steps"):
                    from app.agents.planner import PlanManager

                    PlanManager.mark_action_done(state["plan_steps"], "retrieve")
                    _trigger = (
                        len(state["retrieved_docs"]) == 0
                        or _same_decision_count >= 2
                    )
                    _observation = (
                        f"第{state['iteration']}轮检索到 "
                        f"{len(state['retrieved_docs'])} 篇文档"
                    )
                    if await self._maybe_replan(state, _observation, _trigger):
                        yield SSEEvent(
                            data={
                                "content": "观察结果偏离计划，已重新规划剩余步骤",
                                "iteration": state["iteration"],
                                "replan_count": state.get("replan_count", 0),
                            },
                            event=SSEEventType.THINKING,
                        )
                continue
            if decision == "tool_call":
                # P0-3: 压栈记录 tool_call 节点 Span — 生产路径直接调用
                # _tool_call_streaming（不经 @trace_node 装饰的 _tool_call），
                # 节点 Span 在此显式开启，具体工具执行挂为其子 Span。
                node_span_id: str | None = None
                if self._trace_ctx is not None:
                    node_span_id = self._trace_ctx.start_span(
                        name=f"tool_call_iter{state['iteration']}",
                        span_type=map_span_type("tool_call"),
                        input_data={"iteration": state["iteration"]},
                    )
                _tc_t0 = time.perf_counter()
                _tc_error: str | None = None
                try:
                    # P0-3: yield tool_call_start/end 事件
                    # P1-4: 传入 db / user_uuid 以支持审批记录持久化
                    async for event in self._tool_call_streaming(
                        state, db=db, user_uuid=user_uuid
                    ):
                        yield event
                except Exception as exc:
                    _tc_error = str(exc)
                    raise
                finally:
                    if self._trace_ctx is not None and node_span_id is not None:
                        self._trace_ctx.end_span(
                            node_span_id,
                            name=f"tool_call_iter{state['iteration']}",
                            metadata={
                                "latency_ms": round(
                                    (time.perf_counter() - _tc_t0) * 1000, 2
                                ),
                                "error": _tc_error,
                                "tool_results": len(state["tool_results"]),
                            },
                        )
                # P0-Opt2 + P1-Opt3: 只追加最新工具结果摘要（经去重），不重传历史结果
                if state["tool_results"]:
                    latest = state["tool_results"][-1]
                    raw_summary = self._summarize(latest)
                    tool_name = latest.get("tool", "unknown") if isinstance(latest, dict) else "unknown"

                    # P1-1: 大工具结果写盘 — 原始结果超阈值时写盘，上下文只留 placeholder。
                    # 显著降低 L3（历史/工具日志）体量；Agent 需要全文时用
                    # read_tool_result 按需读回。写盘失败静默降级为原摘要。
                    raw_content = ""
                    if isinstance(latest, dict):
                        raw_content = str(
                            latest.get("result") or latest.get("content") or ""
                        )
                    elif isinstance(latest, str):
                        raw_content = latest
                    if len(raw_content) > _SPILL_TOOL_RESULT_THRESHOLD:
                        try:
                            tenant_id = str(state.get("tenant_id") or "default")
                            spill_ref = self._spill_store.spill(
                                tenant_id, tool_name, raw_content
                            )
                            raw_summary = (
                                f"已写盘 {spill_ref}（可用 read_tool_result 读取全文）"
                            )
                        except Exception as _spill_exc:
                            log.warning(
                                "engine.tool_result_spill_error",
                                error=str(_spill_exc),
                            )

                    # P1-Opt3: 跨轮去重 — 重复结果替换为指针引用
                    deduped = self._dedup.register(
                        turn=state["iteration"],
                        tool_name=tool_name,
                        result_content=raw_summary,
                    )
                    # P0-3/P1-7: 兜圈信号 — 重复工具结果被指针引用，
                    # 连续多次命中说明 Agent 在原地兜圈，触发告警日志。
                    if isinstance(deduped, str) and deduped.startswith("↑"):
                        state["_dedup_hits"] = state.get("_dedup_hits", 0) + 1
                        if state["_dedup_hits"] >= 2:
                            log.warning(
                                "engine.repeated_tool_results",
                                alert=True,
                                alert_type="agent_loop_repeated_results",
                                dedup_hits=state["_dedup_hits"],
                                tool=tool_name,
                                iteration=state["iteration"],
                                session_id=state["session_id"],
                            )
                    state["messages"].append(
                        {
                            "role": "user",
                            "content": f"[系统] 工具结果：{deduped}",
                        }
                    )
                    # P3-E: Scratchpad 追加工具调用笔记
                    _sp = state.get("scratchpad", "")
                    _note = deduped[:80] if isinstance(deduped, str) else ""
                    state["scratchpad"] = _sp + f"\n[轮{state['iteration']}] tool_call: {tool_name} → {_note}"

                    # P1-9: 推进计划状态 + 偏离检测
                    # （触发器：工具结果重复指针命中 / 决策重复）
                    if state.get("plan_steps"):
                        from app.agents.planner import PlanManager

                        PlanManager.mark_action_done(state["plan_steps"], "tool_call")
                        _trigger = (
                            isinstance(deduped, str) and deduped.startswith("↑")
                        ) or _same_decision_count >= 2
                        _observation = (
                            f"第{state['iteration']}轮工具 {tool_name} 返回：{_note}"
                        )
                        if await self._maybe_replan(state, _observation, _trigger):
                            yield SSEEvent(
                                data={
                                    "content": "观察结果偏离计划，已重新规划剩余步骤",
                                    "iteration": state["iteration"],
                                    "replan_count": state.get("replan_count", 0),
                                },
                                event=SSEEventType.THINKING,
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

    def _record_shortcut_trace(
        self,
        *,
        name: str,
        span_type: str,
        query: str,
        user_id: str,
        session_id: str,
        output: str,
        metadata: dict[str, Any],
    ) -> None:
        """P1-3: 短路路径（缓存/FAQ 命中）记录最小 Trace。

        这些路径在完整 TraceContext 创建前即返回，此前完全无追踪记录，
        评测无法观测短路命中率与短路答案。此处创建一次性 TraceContext
        记录单个闭合 Span 后立即 finalize。任何失败静默降级。
        """
        try:
            ctx = TraceContext(
                trace_name=f"rag_shortcut_{name}",
                session_id=session_id,
                user_id=user_id,
                metadata={"query": query[:200]},
            )
            ctx.start()
            ctx.span(
                name=name,
                span_type=span_type,
                input_data={"query": query[:200]},
                output_data=output,
                metadata=metadata,
            )
            ctx.finalize(output=output, metadata=metadata)
        except Exception as exc:
            log.warning("engine.shortcut_trace_error", error=str(exc))

    def _record_compaction_evidence(self, state: AgentState) -> None:
        """P0-2: 记录上下文压缩证据 Span（compaction_event）。

        从 ContextBudgetManager 的最近快照提取压缩前后对比，
        以事后闭合 Span 记录（父节点为当前栈顶 — 压缩发生在 think 压栈前，
        故挂到 task run 根 Span 下）。recorder 不可用时零开销。

        P2-8: 写入 ``preserved_refs`` —— 压缩后仍出现在上下文消息中的文档引用，
        供 compute_context_metrics 的 robustness 维度判定约束保留率。判定方式：
        state["retrieved_docs"] 中文档内容前缀出现在 after 消息文本里即视为保留
        （tail 区原文保留 + 摘要区指针引用）。无文档内容命中时为空列表。
        """
        if self._trace_ctx is None:
            return
        snapshot = self._budget.get_last_snapshot()
        if not snapshot:
            return
        after_msgs = snapshot.get("after") or []
        summary_preview = ""
        for m in after_msgs:
            content = str(m.get("content", ""))
            if "早期上下文摘要" in content:
                summary_preview = content[:200]
                break
        before_count = len(snapshot.get("before") or [])

        # P2-8: 提取压缩后仍保留在上下文中的文档引用
        after_text = "\n".join(str(m.get("content", "")) for m in after_msgs)
        preserved_refs: list[str] = []
        for doc in state.get("retrieved_docs", []) or []:
            if not isinstance(doc, dict):
                continue
            rid = str(doc.get("doc_id") or doc.get("chunk_id") or "")
            content = str(doc.get("content", ""))
            # 文档内容前缀（60 字）出现在 after 消息中即视为保留
            if rid and content and content[:60] in after_text:
                preserved_refs.append(rid)

        self._trace_ctx.span(
            name="context_compaction",
            span_type="context.compact",
            metadata={
                "compaction_event": {
                    "reason": "token_budget_exceeded",
                    "kept_summary": summary_preview,
                    "dropped_content_types": ["middle_messages"],
                    "dropped_message_count": max(0, before_count - len(after_msgs)),
                    "preserved_refs": preserved_refs,
                },
                "token_cost": {
                    "compact_before_tokens": int(snapshot.get("before_tokens", 0)),
                    "compact_after_tokens": int(snapshot.get("after_tokens", 0)),
                },
            },
        )

    async def _maybe_replan(
        self,
        state: AgentState,
        observation: str,
        trigger: bool,
    ) -> bool:
        """P1-9: 启发式触发器命中时执行偏离检测，必要时仅重规划剩余步骤。

        成本控制：
            - 未触发（trigger=False）时零 LLM 调用；
            - 每会话最多重规划 PlanManager.max_replans 次；
            - 偏离度 < 阈值时不改动计划。

        Args:
            state: Agent Loop 状态。
            observation: 最新观察结果摘要。
            trigger: 启发式触发器是否命中（检索为空 / dedup 指针 / 决策重复）。

        Returns:
            True 表示发生了重规划。
        """
        if not trigger or self._planner is None:
            return False
        plan = state.get("plan_steps")
        if not plan:
            return False
        if state.get("replan_count", 0) >= self._planner.max_replans:
            log.info(
                "engine.replan_cap_reached",
                replan_count=state["replan_count"],
                session_id=state["session_id"],
            )
            return False

        from app.agents.planner import DEVIATION_THRESHOLD

        deviation = await self._planner.assess_deviation(
            state["query"], plan, observation
        )
        if deviation < DEVIATION_THRESHOLD:
            return False

        state["plan_steps"] = await self._planner.replan_remaining(
            state["query"], plan, observation
        )
        state["replan_count"] = state.get("replan_count", 0) + 1
        log.info(
            "engine.replanned",
            deviation=deviation,
            replan_count=state["replan_count"],
            kept_done=sum(1 for s in plan if s.get("status") == "done"),
            session_id=state["session_id"],
        )
        return True

    async def _think_with_timeout(self, state: AgentState) -> str | None:
        """带单步骤超时的 think 调用（P1-7 卡死防护）。

        think 是 Agent Loop 中最可能卡死的节点（LLM 无响应）。
        超时时返回 None，由调用方跳出决策循环进入生成兜底，
        避免无限等待导致整个请求挂起。
        """
        try:
            return await asyncio.wait_for(
                self._think(state),
                timeout=self._step_timeout_s,
            )
        except asyncio.TimeoutError:
            state["_step_timeouts"] = state.get("_step_timeouts", 0) + 1
            log.warning(
                "engine.step_timeout",
                alert=True,
                alert_type="agent_step_timeout",
                step="think",
                timeout_s=self._step_timeout_s,
                iteration=state.get("iteration", 0),
                session_id=state.get("session_id", ""),
            )
            return None
        except Exception:
            raise

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

        # P3-E: 注入 Scratchpad 推理笔记（高密度信息，帮助 LLM 理解上下文）
        scratchpad = state.get("scratchpad", "")
        if scratchpad:
            # 截断到最近 300 字，避免 Scratchpad 自身膨胀
            recent_scratchpad = scratchpad[-300:] if len(scratchpad) > 300 else scratchpad
            dynamic_parts.append(f"\n推理笔记：\n{recent_scratchpad}")

        # P1-9: 注入计划状态视图 — LLM 决策时感知全局进度（已完成 / 剩余步骤）
        plan = state.get("plan_steps")
        if plan:
            from app.agents.planner import PlanManager

            brief = PlanManager.format_plan_brief(plan)
            if brief:
                dynamic_parts.append(f"执行计划：{brief}")

        # P4-E: 注入对话焦点 — 让 LLM 感知当前话题和实体
        focus = state.get("conversation_focus")
        if focus:
            dynamic_parts.append(
                f"当前对话焦点：主题={focus.get('topic', '')}, "
                f"实体={focus.get('entity', '')}, 意图={focus.get('intent', '')}"
            )

        # P4-E: 漂移警告 — 用户可能切换了话题
        drift = state.get("drift_info")
        if drift and drift.get("is_drift"):
            dynamic_parts.append(
                "注意：用户可能切换了话题，请关注当前问题的独立完整性。"
            )

        dynamic_parts.append("请决定下一步。")
        dynamic_context = "，".join(dynamic_parts)

        messages: list[Message] = list(base_messages) + [
            {"role": "user", "content": dynamic_context}
        ]

        # P0-1: 尾部约束提醒（首尾三明治）— 追加到上下文末尾，形成注意力尾部高地。
        # 无论中间历史/工具结果如何膨胀，模型生成下一个决策前注意力必扫过尾部，
        # 保证"仅检索已发布文档/禁止越权/不虚构"等核心约束始终被看见。
        if _CONSTRAINT_REMINDER:
            messages = messages + [
                {"role": "user", "content": _CONSTRAINT_REMINDER}
            ]

        try:
            text = ""
            async for chunk in self.llm.chat(messages, stream=False):
                # P0-Stage2: 捕获 usage dict 累加到引擎用量记录
                if isinstance(chunk, dict) and chunk.get("type") == "usage":
                    self._accumulated_usage["input_tokens"] += chunk.get("input_tokens", 0)
                    self._accumulated_usage["output_tokens"] += chunk.get("output_tokens", 0)
                    if chunk.get("model"):
                        self._accumulated_usage["model"] = chunk["model"]
                    # P0-3: 同步到四轴硬预算累积器（think 调用的 tokens + cost）
                    _think_in = chunk.get("input_tokens", 0)
                    _think_out = chunk.get("output_tokens", 0)
                    _think_cost_usd = self._estimate_cost_cents(
                        chunk.get("model", ""),
                        _think_in,
                        _think_out,
                    ) / 100.0  # 分 → 美元
                    self._run_budget.add_usage(
                        input_tokens=_think_in,
                        output_tokens=_think_out,
                        cost_usd=_think_cost_usd,
                    )
                    continue
                if isinstance(chunk, str):
                    text += chunk
            decision = self._parse_decision(text)
            # P1-3: 保留 LLM 原始决策文本（截断 500 字）作为 think Span 证据 —
            # _parse_decision 只留路由词，评测无法回溯"为什么选这条路"；
            # 由 @trace_node 在 Span 闭合时合并进 metadata。
            state["_span_evidence"] = {
                "raw_decision_text": text[:500],
                "decision": decision,
            }
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

    def _build_recall_evaluator(
        self, kb_ids: list[str] | None
    ) -> "Callable[[str], Awaitable[float]]":
        """构建召回质量评估器 — P1-10 在线回退双跑用。

        以小 top_k 检索召回，返回候选分数均值（空召回为 0.0）。
        注意：此处评估的是"召回面"质量，刻意不做权限过滤与重排 ——
        双跑两侧同口径对比即公平，且避免重复消耗重排算力。
        """

        async def _evaluate(q: str) -> float:
            docs = await self.retriever.search(
                q, kb_ids=kb_ids, top_k=_FALLBACK_EVAL_TOP_K
            )
            if not docs:
                return 0.0
            scores = [float(d.get("score", 0.0)) for d in docs]
            return sum(scores) / len(scores)

        return _evaluate

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
        # P2-B: 优先使用重写后的查询进行检索，回退到原始查询
        query = state.get("rewritten_query") or state["query"]
        original_query = state["query"]

        # 显式空 kb_ids（用户无任何可访问知识库）— 短路返回空，
        # 不得把空列表传给底层检索器（底层对空列表按"不过滤"处理，
        # 会回落为全库检索造成跨知识库泄漏）。
        if kb_ids is not None and len(kb_ids) == 0:
            log.info(
                "engine.retrieve.no_accessible_kb",
                iteration=state["iteration"],
            )
            state["retrieved_docs"] = []
            # P0-2: 短路路径同样产出选择证据（空纳入），避免上下文评分误判
            # P1-4: 记录 kb_ids + 版本快照时间戳，支持回放追溯
            state["_span_evidence"] = {
                "source": "knowledge_base",
                "included_refs": [],
                "excluded_refs": [],
                "no_accessible_kb": True,
                "kb_ids": kb_ids or [],
                "kb_version_snapshot": state.get("kb_version_snapshot"),
            }
            return

        # 1. 多路检索召回候选
        # P0 wiki 层级：filters 从 state 取（answer 初始化时写入），透传给
        # retriever.search，由 filter_builder 转为后端 filter 子句。
        hierarchy_filters = state.get("filters")
        candidates = await self.retriever.search(
            query, kb_ids=kb_ids, top_k=_RETRIEVE_TOP_K, filters=hierarchy_filters
        )
        log.info(
            "engine.retrieve.candidates",
            count=len(candidates),
            iteration=state["iteration"],
            query_used=query[:100],
            is_rewritten=query != original_query,
        )

        # 1.5 P1-4: 检索结果注入扫描 — HybridRetriever 返回后扫描 prompt injection
        # 命中文档进 state["quarantined_docs"]（不丢弃，保留审计证据），
        # 安全文档继续走权限过滤 → 重排 → 生成。
        # 扫描失败时优雅降级：放行全部文档，仅记录日志（不阻塞 RAG 主流程）。
        if self._injection_guard is not None and candidates:
            try:
                scan_result = self._injection_guard.scan(
                    candidates, query=original_query
                )
                candidates = scan_result.safe_docs
                # 累积隔离文档到 state（多轮检索可能叠加）
                quarantined = state.get("quarantined_docs") or []
                quarantined.extend(scan_result.quarantined_docs)
                state["quarantined_docs"] = quarantined
                if scan_result.total_quarantined > 0:
                    log.warning(
                        "engine.retrieve.injection_quarantined",
                        scanned=scan_result.total_scanned,
                        quarantined=scan_result.total_quarantined,
                        hits=scan_result.total_hits,
                        iteration=state["iteration"],
                    )
            except Exception as exc:
                # 优雅降级：扫描异常不影响主流程，保留原 candidates
                log.warning(
                    "engine.retrieve.injection_scan_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # 2. ABAC 权限过滤（必须在重排之前！）
        # 请求级过滤器（携带当前用户上下文）优先于构造级注入 —
        # 引擎为全局单例，构造级过滤器无法区分请求用户。
        active_filter = state.get("permission_filter") or self.permission_filter
        filtered = candidates
        if active_filter is not None:
            try:
                filtered = await active_filter(candidates)
                log.info(
                    "engine.retrieve.permission_filtered",
                    before=len(candidates),
                    after=len(filtered),
                )
            except Exception as exc:
                log.error("engine.retrieve.permission_error", error=str(exc))
                # 权限过滤出错时保守处理：返回空，避免泄露越权文档
                filtered = []

        # 3. 重排 — 使用原始用户查询（非重写查询）进行重排
        # HyDE 生成的是文档而非查询，不适合做重排输入
        if filtered:
            try:
                reranked = await self.reranker.rerank(
                    query=original_query,
                    documents=filtered,
                    top_k=_RERANK_TOP_K,
                )
                # 将重排分数回填到原文档，并按重排顺序排列
                state["retrieved_docs"] = self._apply_rerank_scores(filtered, reranked)

                # 质量守卫：检查重排分数均值，低质量时扩展 top_k 重排
                if self._quality_guard is not None:
                    # P2: 动态匹配阈值 — 记录查询频次（仅首次迭代，避免多轮重复计数）
                    if state.get("iteration", 1) == 1:
                        try:
                            await self._quality_guard.record_query_frequency(
                                original_query
                            )
                        except Exception:
                            pass
                    # P2: 获取频率自适应动态阈值（不可用时回退静态阈值）
                    _dyn_threshold: float | None = None
                    try:
                        _dyn_threshold = (
                            await self._quality_guard.get_dynamic_threshold(
                                original_query
                            )
                        )
                    except Exception:
                        _dyn_threshold = None

                    check_result = self._quality_guard.check_retrieval_quality(
                        state["retrieved_docs"],
                        threshold_override=_dyn_threshold,
                    )
                    if self._quality_guard.should_retry_retrieval(
                        check_result, self._retrieval_retry_count
                    ):
                        self._retrieval_retry_count += 1
                        expanded_top_k = self._quality_guard.get_expanded_top_k()
                        log.info(
                            "engine.retrieve.quality_retry",
                            mean_score=check_result.mean_score,
                            expanded_top_k=expanded_top_k,
                            retry_count=self._retrieval_retry_count,
                        )
                        reranked = await self.reranker.rerank(
                            query=original_query,
                            documents=filtered,
                            top_k=expanded_top_k,
                        )
                        state["retrieved_docs"] = self._apply_rerank_scores(
                            filtered, reranked
                        )
                        # P1-2: 重试后重新检查质量
                        recheck = self._quality_guard.check_retrieval_quality(
                            state["retrieved_docs"],
                            threshold_override=_dyn_threshold,
                        )
                        if self._quality_guard.should_reject_after_retry(
                            recheck, self._retrieval_retry_count
                        ):
                            state["retrieval_insufficient"] = True
                    elif self._quality_guard.should_reject_after_retry(
                        check_result, self._retrieval_retry_count
                    ):
                        # P1-2: 首次检查就低于阈值且已无重试次数 → 标记拒答
                        state["retrieval_insufficient"] = True
            except Exception as exc:
                log.warning("engine.retrieve.rerank_error", error=str(exc))
                state["retrieved_docs"] = filtered[:_RERANK_TOP_K]
        else:
            state["retrieved_docs"] = []

        # 4. P0-4 时间新鲜度 — 生效窗口硬过滤 + 平局组按 updated_at 裁决
        # 新旧规范冲突场景：已过失效时间的旧规范不再进入上下文；
        # 重排分数相近（tie band 内）时新版本排前，分数差距大时顺序不变。
        if state["retrieved_docs"]:
            try:
                from app.config import get_settings as _gs

                if getattr(_gs(), "RECENCY_BOOST_ENABLED", True):
                    from app.rag.recency import (
                        apply_recency_boost,
                        filter_by_validity_window,
                    )

                    state["retrieved_docs"] = apply_recency_boost(
                        filter_by_validity_window(state["retrieved_docs"])
                    )
            except Exception as exc:
                # 优雅降级：recency 处理失败不影响已召回结果
                log.warning("engine.retrieve.recency_error", error=str(exc))

        # 4.5 P0 外部文档实时回源校验 — 仅对强时效外部文档做轻量探测 + 按需拉取
        # 消除窗口期：用户检索命中外部文档时实时回源校验是否已更新。
        # 策略：批量过滤需要校验的文档 → 逐个校验（最多 3 个）→ 用最新内容覆盖。
        if state["retrieved_docs"]:
            try:
                state["retrieved_docs"] = await self._verify_external_docs(
                    state["retrieved_docs"]
                )
            except Exception as exc:
                # 优雅降级：外部同步失败不影响已召回结果
                log.warning(
                    "engine.retrieve.external_sync_error",
                    error=str(exc)[:200],
                )

        # P1-4 冲突裁决 — 同 key 文档按权威序取一，其余剔除。
        # 企业知识库多级制度（公司/部门/项目）冲突时，仅当文档携带
        # authority_key/conflict_key 与 authority 元数据才参与裁决；
        # 无冲突元数据的文档原样保留（不改变既有行为）。
        try:
            state["retrieved_docs"] = self._resolve_doc_conflicts(
                state["retrieved_docs"]
            )
        except Exception as exc:
            log.warning("engine.retrieve.conflict_resolve_error", error=str(exc))

        # P0-2: 上下文选择证据 — 写入 _span_evidence，由 @trace_node 装饰器
        # 在 Span 闭合时合并进 metadata，供 ContextTraceRecord.from_spans 聚合
        # （included_refs / excluded_refs / trust_levels / token_cost）。
        def _ref_id(d: dict[str, Any]) -> str | None:
            rid = d.get("doc_id") or d.get("chunk_id")
            return str(rid) if rid else None

        included_ids = [
            rid
            for d in state["retrieved_docs"]
            if isinstance(d, dict)
            for rid in [_ref_id(d)]
            if rid
        ]
        filtered_id_set = {
            rid
            for d in filtered
            if isinstance(d, dict)
            for rid in [_ref_id(d)]
            if rid
        }
        excluded_ids = [
            rid
            for d in candidates
            if isinstance(d, dict)
            for rid in [_ref_id(d)]
            if rid and rid not in filtered_id_set
        ]
        # included_contents 为截断摘要（每条 500 字符），供离线评测的
        # RAGAS / Judge 在不重复检索的前提下拿到上下文内容（P1-1）。
        included_contents = [
            str(d.get("content", ""))[:500]
            for d in state["retrieved_docs"]
            if isinstance(d, dict) and d.get("content")
        ]
        state["_span_evidence"] = {
            "source": "knowledge_base",
            "included_refs": included_ids,
            "included_contents": included_contents,
            "excluded_refs": excluded_ids,
            "trust_levels": {rid: "internal" for rid in included_ids},
            "token_cost": {
                "context_load_tokens": sum(len(c) for c in included_contents) // 4
            },
            "permission_filtered_count": max(0, len(candidates) - len(filtered)),
            # P1-4: 记录知识库版本号 — kb_ids + 版本快照时间戳，
            # 支持回放"这次错误由哪一版资料造成"。
            "kb_ids": kb_ids or [],
            "kb_version_snapshot": state.get("kb_version_snapshot"),
            # P1-4: 注入扫描隔离记录 — 命中 prompt injection 的文档引用 + 命中模式
            # 仅记录引用 ID 与模式，不携带命中原文（避免敏感内容扩散到 trace）。
            "injection_quarantined_refs": [
                rid
                for d in (state.get("quarantined_docs") or [])
                if isinstance(d, dict)
                for rid in [_ref_id(d)]
                if rid
            ],
            "injection_hit_patterns": sorted({
                h.get("pattern_id")
                for d in (state.get("quarantined_docs") or [])
                if isinstance(d, dict)
                for h in (d.get("injection_hits") or [])
                if h.get("pattern_id")
            }),
        }

        log.info(
            "engine.retrieve.done",
            final_count=len(state["retrieved_docs"]),
            iteration=state["iteration"],
        )

    async def _verify_external_docs(
        self,
        docs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """P0: 对召回的外部文档做实时回源校验。

        策略（避免大量回源拖慢响应）：
        - 批量查询所有 doc_id 的元数据（一次 DB 查询）
        - 仅校验 source != NULL + category ∈ 强时效类 + 缓存过期 的文档
        - 每次检索最多校验 3 个文档
        - 校验后用最新内容覆盖 chunk content，并写入 sync_status 用于 P3 prompt
        """
        import uuid as _uuid

        from app.services.external_sync_service import get_external_sync_service

        sync_service = get_external_sync_service()

        # 提取所有 doc_id（用于批量查询）
        doc_ids: list[_uuid.UUID] = []
        doc_id_to_doc: dict[str, dict[str, Any]] = {}

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            doc_id = doc.get("doc_id")
            if not doc_id:
                continue
            try:
                uid = _uuid.UUID(str(doc_id))
                doc_ids.append(uid)
                doc_id_to_doc[str(doc_id)] = doc
            except (ValueError, TypeError):
                continue

        if not doc_ids:
            return docs

        # 批量查询需要校验的文档 ID
        try:
            to_verify = await sync_service.filter_docs_to_verify(doc_ids)
        except Exception as exc:
            log.warning(
                "engine.retrieve.external_filter_error",
                error=str(exc)[:200],
            )
            return docs

        if not to_verify:
            return docs

        # 逐个校验（最多 3 个，避免拖慢响应）
        max_verify = 3
        verified_count = 0

        for doc_id in to_verify:
            if verified_count >= max_verify:
                break
            doc = doc_id_to_doc.get(str(doc_id))
            if not doc:
                continue

            try:
                result = await sync_service.verify_and_refresh(doc_id)
                verified_count += 1
            except Exception as exc:
                log.warning(
                    "engine.retrieve.external_verify_error",
                    doc_id=str(doc_id),
                    error=str(exc)[:200],
                )
                doc["sync_status"] = "verify_failed"
                continue

            # 写入 sync_status 用于 P3 prompt 时效声明
            doc["sync_status"] = result.sync_status
            # 如果内容已更新，用最新内容覆盖本次回答
            if result.status == "updated" and result.content:
                doc["content"] = result.content
            # 写入 source_url 用于 P3 prompt 引用展示
            if result.source_url:
                doc["source_url"] = result.source_url

        log.info(
            "engine.retrieve.external_sync_verified",
            total_docs=len(docs),
            candidates=len(to_verify),
            verified=verified_count,
        )
        return docs

    @staticmethod
    def _apply_rerank_scores(
        docs: list[dict[str, Any]],
        reranked: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将重排结果（index+score）回填到原文档列表，按重排顺序输出。

        只保留重排器选中的 top_k 结果 — 不补回未入选候选：
        补回会让被重排器截断的低分文档重新进入生成上下文，
        抵消重排的筛选作用并浪费上下文预算。
        （重排器异常时由调用方回退 ``filtered[:_RERANK_TOP_K]``。）
        """
        result: list[dict[str, Any]] = []
        for item in reranked:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs):
                doc = dict(docs[idx])
                doc["score"] = item.get("score", doc.get("score", 0.0))
                doc["rerank_content"] = item.get("content", doc.get("content", ""))
                result.append(doc)
        return result

    @staticmethod
    def _doc_ref(doc: dict[str, Any]) -> str:
        """返回文档的唯一引用标识（用于冲突裁决回映射）。"""
        rid = str(doc.get("doc_id") or doc.get("chunk_id") or doc.get("id") or "")
        if rid:
            return rid
        return str(doc.get("title") or f"doc-{id(doc)}")

    def _resolve_doc_conflicts(
        self,
        docs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """P1-4 冲突裁决 — 同 key 文档按权威序取一，其余剔除。

        企业知识库多级制度（公司/部门/项目）冲突时落地用法：文档上打标
        ``conflict_key`` / ``authority_key``（冲突所在字段，如 "报销上限"）
        与 ``authority``（来源权威，如 system_rule / tool_fact），同一
        冲突键下多篇文档按 系统规则>用户输入>工具事实>已确认摘要>模型推测
        裁决，保留权威者、剔除其余；同 key 同权威时按 last win（时间新旧）。

        无冲突键 / 无法裁决的文档原样保留（不改变既有行为，保证零侵入）。

        Args:
            docs: 检索并重排后的文档列表。

        Returns:
            冲突裁决后的文档列表（顺序保持）。
        """
        if not docs:
            return docs

        # 收集携带冲突键的文档
        keyed: list[tuple[int, dict[str, Any]]] = [
            (i, d) for i, d in enumerate(docs)
            if (d.get("conflict_key") or d.get("authority_key"))
        ]
        if not keyed:
            return docs

        from app.context.conflict_resolver import ConflictClaim, ConflictResolver

        resolver = ConflictResolver()
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for i, d in keyed:
            k = d.get("conflict_key") or d.get("authority_key")
            groups.setdefault(str(k), []).append((i, d))

        resolved_indices: set[int] = set(range(len(docs)))  # 默认全部保留
        for k, group in groups.items():
            if len(group) < 2:
                continue
            idx_list = [i for i, _ in group]
            # 参与裁决的成员先全部剔除，再保留胜出者
            for i in idx_list:
                resolved_indices.discard(i)

            claims = [
                ConflictClaim(
                    value=str(d.get("content") or "")[:200],
                    authority=d.get("authority", "tool_fact"),
                    source=self._doc_ref(d),
                    timestamp=d.get("updated_at") or d.get("version_date"),
                    key=k,
                )
                for _, d in group
            ]
            resolution = resolver.resolve(claims, same_key_only=True)
            if resolution is None:
                # 无法裁决（如权威未知）→ 全部保留
                resolved_indices.update(idx_list)
                continue
            winner_idx = next(
                (i for i, d in group if self._doc_ref(d) == resolution.winner_source),
                idx_list[0],
            )
            resolved_indices.add(winner_idx)

        result = [d for i, d in enumerate(docs) if i in resolved_indices]
        if len(result) != len(docs):
            log.info(
                "engine.conflict_resolved",
                original=len(docs),
                after=len(result),
                groups=len(groups),
            )
        return result

    # ------------------------------------------------------------------
    # tool_call：通过 MCP Client 调用工具
    # ------------------------------------------------------------------

    @trace_node("tool_call")
    async def _tool_call(self, state: AgentState) -> None:
        """通过 MCP Client 调用企业系统工具（非流式版本，向后兼容）。

        委托给 ``_tool_call_streaming`` 并排空事件流。
        供 LangGraph 图节点 ``_graph_tool_call`` 及测试用例调用。

        Find Skills 渐进式技能加载：
            - SKILL_FINDER_ENABLED=True 时，先从用户查询匹配相关技能，
              只加载匹配工具的完整 schema（按需加载，节省 token）；
            - SKILL_FINDER_ENABLED=False 或匹配失败时，fallback 到全量加载。
        """
        # P1-4: LangGraph 路径不传 db/user_uuid（审批仅支持默认路径）
        async for _ in self._tool_call_streaming(state):
            pass

    async def _tool_call_streaming(
        self,
        state: AgentState,
        db: Any = None,
        user_uuid: uuid.UUID | None = None,
    ) -> AsyncIterator[SSEEvent]:
        """通过 MCP Client 调用工具 — 流式 yield tool_call_start/end 事件。

        P0-3 核心：在每个工具调用前后推送 SSE 事件，让用户看到工具执行进度。

        P1-4 更新：接收 ``db`` / ``user_uuid`` 参数并透传给 ``_execute_tool_use``，
        当危险工具被拦截时 yield ``approval_required`` SSE 事件。

        事件流：
            - tool_call_start：工具开始执行（含 tool_name / arguments）
            - approval_required：危险工具需要用户确认（P1-4，可选）
            - tool_call_end：工具执行完成（含 result / duration_ms / status）

        工具调用守卫（DangerousToolGuard）在 ``_execute_tool_use`` 内部处理：
            - 只读工具 → 直接放行；
            - 危险工具 → 需要用户确认（P1 持久化审批 + approval_required 事件）；
            - 未确认的危险工具被阻断，返回结构化错误给 LLM。
        """
        # Find Skills 渐进式技能加载 — 按需加载工具 schema
        tools = await self._get_tools_for_query(state["query"])

        if not tools:
            log.info("engine.tool_call.no_tools")
            return

        # P2: 构建「无匹配工具」强制选项提示词
        # 让 LLM 在所有候选工具都不适用时显式声明，而非硬凑一个工具调用。
        # 这是发现检索漏检的核心机制，把隐性的"看不见"错误变成显性信号。
        tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        no_match_instruction = (
            "根据用户问题选择合适的工具调用。如无需调用工具，直接回复原文。\n"
            "重要：如果以上候选工具都无法满足用户需求，"
            "请不要硬凑工具调用，直接回复原文并说明无可用工具。\n"
            f"当前可用工具：{', '.join(tool_names) if tool_names else '无'}"
        )

        messages: list[Message] = [
            {
                "role": "system",
                "content": no_match_instruction,
            },
            {"role": "user", "content": state["query"]},
        ]

        try:
            async for chunk in self.llm.chat(messages, tools=tools, stream=False):
                if isinstance(chunk, dict) and chunk.get("type") == "tool_use":
                    tool_name = chunk.get("name", "unknown")
                    tool_use_id = chunk.get("id", "")
                    tool_input = chunk.get("input", {})

                    # yield tool_call_start 事件
                    yield SSEEvent(
                        data={
                            "tool_name": tool_name,
                            "tool_use_id": tool_use_id,
                            "arguments": tool_input,
                        },
                        event=SSEEventType.TOOL_CALL_START,
                    )

                    # 执行工具并计时
                    # P1-4: _execute_tool_use 改为 async generator，
                    # 可能 yield approval_required 事件（危险工具被拦截时）
                    start_time = time.monotonic()
                    approval_required = False
                    async for approval_event in self._execute_tool_use(
                        state, chunk, db=db, user_uuid=user_uuid
                    ):
                        yield approval_event
                        approval_required = True
                    duration_ms = int((time.monotonic() - start_time) * 1000)

                    # 获取工具执行结果摘要和状态
                    latest_result = (
                        state["tool_results"][-1] if state["tool_results"] else None
                    )
                    result_summary = ""
                    status = "success"
                    if approval_required:
                        # P1-4: 危险工具被拦截，等待用户审批
                        status = "approval_required"
                    elif latest_result and isinstance(latest_result, dict):
                        result_str = str(latest_result.get("result", ""))
                        result_summary = result_str[:500]
                        if '"error"' in result_str or '"blocked_by_guard"' in result_str:
                            status = "error"

                    # yield tool_call_end 事件
                    yield SSEEvent(
                        data={
                            "tool_use_id": tool_use_id,
                            "tool_name": tool_name,
                            "result": result_summary,
                            "duration_ms": duration_ms,
                            "status": status,
                        },
                        event=SSEEventType.TOOL_CALL_END,
                    )
        except Exception as exc:
            log.error("engine.tool_call.error", error=str(exc))

    async def _get_tools_for_query(self, query: str) -> list[Tool]:
        """获取工具列表 — Find Skills 按需加载或全量加载。

        Args:
            query: 用户查询，用于技能匹配。

        Returns:
            传给 LLM 的 Tool 列表。
        """
        # Find Skills 渐进式技能加载
        if self._skill_finder is not None and self._skill_registry is not None:
            try:
                # 首次调用时从 MCP Server 构建轻量技能索引（延迟加载）
                if not self._skill_registry.get_all_names():
                    self._skill_registry.load_from_server(self.mcp._server)

                # P0-1: 向量召回通道 — 首次索引构建后预计算技能描述向量
                embedder = None
                if (
                    not self._skill_registry.get_all_embeddings()
                    and self._skill_registry.get_all_names()
                ):
                    try:
                        from app.config import get_settings as _gs2

                        if getattr(_gs2(), "SKILL_VECTOR_RECALL_ENABLED", True):
                            from app.llm.embedder import get_embedder

                            embedder = get_embedder()
                            await self._skill_registry.build_embeddings(embedder)
                    except Exception as exc:
                        log.warning("engine.skill_embed_build_failed", error=str(exc))
                        embedder = None
                elif self._skill_registry.get_all_embeddings():
                    try:
                        from app.llm.embedder import get_embedder

                        embedder = get_embedder()
                    except Exception:
                        embedder = None

                # 匹配相关技能并按需加载完整 schema（向量通道不可用时自动退化关键词）
                matched_names = await self._skill_finder.afind_relevant_skills(
                    query, embedder=embedder
                )
                if matched_names:
                    tools = await self._skill_registry.load_tools(matched_names)
                    if tools:
                        log.debug(
                            "engine.skill_finder.loaded",
                            query=query[:80],
                            matched=matched_names,
                            loaded=len(tools),
                        )
                        return tools
                    # 加载失败（工具名不存在），fallback 到全量
                    log.warning("engine.skill_finder.load_empty_fallback")
            except Exception as exc:
                log.warning("engine.skill_finder.error", error=str(exc))

        # Fallback: 全量加载所有工具
        try:
            return await self.mcp.get_tools_for_llm()
        except Exception as exc:
            log.warning("engine.tool_call.list_error", error=str(exc))
            return []

    async def _execute_tool_use(
        self,
        state: AgentState,
        tool_use: ToolUse,
        db: Any = None,
        user_uuid: uuid.UUID | None = None,
    ) -> AsyncIterator[SSEEvent]:
        """执行单个 ToolUse — 通过 MCPClient 调用并将结果存入 state。

        P1-4 重构：从 ``async def`` 改为 ``AsyncIterator[SSEEvent]``，
        当危险工具被拦截时 yield ``approval_required`` 事件。

        工具调用守卫（DangerousToolGuard）在执行前拦截危险操作：
            - 只读工具（knowledge_search / document_get 等）→ 直接放行；
            - 危险工具（document_create / create_it_ticket 等）→ 需要用户确认；
              P1-4: 创建 ToolApproval 记录（含 JSONB 状态快照）+ yield
              ``approval_required`` SSE 事件，前端弹窗审批后通过 REST 恢复。
            - 未确认的危险工具被阻断，返回结构化错误给 LLM，不执行真实操作。

        这借鉴 DECO 数仓 Agent 的 beforeTool Hook 设计：
        "prompt 是软约束，不是安全边界。任何不可逆操作都必须有代码级强制确认。"

        Args:
            state: Agent Loop 状态。
            tool_use: LLM 返回的 ToolUse 字典。
            db: 异步数据库会话（P1-4 审批记录持久化，为 None 时跳过审批创建）。
            user_uuid: 当前用户 UUID（P1-4 审批记录归属）。

        Yields:
            SSEEvent: ``approval_required`` 事件（仅危险工具被拦截时）。
        """
        tool_name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})
        tool_use_id = tool_use.get("id", "")

        # P0-3: 工具审计 Span — 每次工具执行（含被守卫拦截）都记录
        # tool.call 标准 Span，使 persist_tool_spans 过滤集合可匹配、
        # 评测轨迹可回溯每次工具调用的参数/状态/结果。
        _audit_span_id: str | None = None
        if self._trace_ctx is not None:
            _audit_span_id = self._trace_ctx.start_span(
                name=f"tool:{tool_name}",
                span_type=SpanType.TOOL_CALL.value,
                metadata={
                    "arguments": (
                        _safe_serialize(tool_input)
                        if isinstance(tool_input, dict)
                        else {"raw": str(tool_input)[:200]}
                    ),
                    "tool_use_id": tool_use_id,
                },
            )
        _audit: dict[str, Any] = {"status": "success", "output": "", "error": None}

        # 工具调用守卫 — beforeTool 拦截（P1: 传入 session_id 实现会话级控制）
        guard_result = self._tool_guard.check(
            tool_name, tool_input, session_id=state.get("session_id")
        )
        if guard_result.needs_confirmation:
            log.warning(
                "engine.tool_call.blocked_by_guard",
                tool=tool_name,
                reason=guard_result.reason,
                irreversible=guard_result.irreversible,
                session_id=state.get("session_id"),
            )

            # P1-4: 创建审批记录（持久化 AgentState 快照）+ yield approval_required 事件
            if db is not None and user_uuid is not None:
                try:
                    # 延迟导入避免循环依赖
                    from app.services.approval_service import ApprovalService

                    approval_service = ApprovalService(db)
                    snapshot = self._serialize_state_for_snapshot(state)
                    tenant_id_str = state.get("tenant_id")
                    tenant_uuid = None
                    if tenant_id_str:
                        try:
                            tenant_uuid = uuid.UUID(tenant_id_str)
                        except (ValueError, TypeError):
                            pass

                    approval = await approval_service.create_approval(
                        user_id=user_uuid,
                        session_id=state["session_id"],
                        tool_name=tool_name,
                        tool_use_id=tool_use_id,
                        tool_arguments=tool_input if isinstance(tool_input, dict) else {},
                        reason=guard_result.reason,
                        irreversible=guard_result.irreversible,
                        agent_state_snapshot=snapshot,
                        tenant_id=tenant_uuid,
                    )
                    log.info(
                        "engine.approval.created",
                        approval_id=str(approval.id),
                        tool=tool_name,
                        session_id=state["session_id"],
                    )

                    # yield approval_required SSE 事件 — 前端接收后弹窗
                    yield SSEEvent(
                        data={
                            "approval_id": str(approval.id),
                            "tool_name": tool_name,
                            "tool_use_id": tool_use_id,
                            "arguments": tool_input,
                            "reason": guard_result.reason,
                            "irreversible": guard_result.irreversible,
                            "session_id": state["session_id"],
                        },
                        event=SSEEventType.APPROVAL_REQUIRED,
                    )
                except Exception as exc:
                    log.error(
                        "engine.approval.create_failed",
                        tool=tool_name,
                        error=str(exc),
                    )
                    # 审批创建失败时仍阻断工具，但不发 approval_required 事件

            # 返回结构化错误给 LLM，告知需要用户确认
            blocked_msg = json.dumps(
                {
                    "error": f"工具 {tool_name} 需要用户确认才能执行",
                    "tool": tool_name,
                    "reason": guard_result.reason,
                    "irreversible": guard_result.irreversible,
                    "action_required": "请在前端确认后重试",
                },
                ensure_ascii=False,
            )
            state["tool_results"].append(
                {
                    "tool": tool_name,
                    "arguments": tool_input,
                    "result": blocked_msg,
                    "content": blocked_msg,
                }
            )
            _audit["status"] = "blocked"
            _audit["output"] = blocked_msg
            self._end_tool_audit_span(_audit_span_id, tool_name, _audit)
            return

        # 未知工具被守卫阻断（deny-by-default）— 不执行，返回结构化错误
        if guard_result.blocked:
            log.warning(
                "engine.tool_call.blocked_unknown",
                tool=tool_name,
                reason=guard_result.reason,
            )
            blocked_msg = json.dumps(
                {
                    "error": f"工具 {tool_name} 被安全守卫阻断：{guard_result.reason}",
                    "tool": tool_name,
                    "reason": guard_result.reason,
                    "action_required": "该工具未在安全/危险清单中，请联系管理员注册",
                },
                ensure_ascii=False,
            )
            state["tool_results"].append(
                {
                    "tool": tool_name,
                    "arguments": tool_input,
                    "result": blocked_msg,
                    "content": blocked_msg,
                }
            )
            _audit["status"] = "blocked"
            _audit["output"] = blocked_msg
            self._end_tool_audit_span(_audit_span_id, tool_name, _audit)
            return

        log.info(
            "engine.tool_call.execute",
            tool=tool_name,
            tool_use_id=tool_use_id,
            guard=guard_result.action.value,
        )
        try:
            # 透传请求级租户 ID — MCP Server 按租户过滤工具内查询，
            # 不信任 LLM 在 tool_input 中自封的租户标识（防跨租户泄漏）
            result = await self.mcp.call_tool(
                tool_name, tool_input, tenant_id=state.get("tenant_id")
            )
            state["tool_results"].append(
                {
                    "tool": tool_name,
                    "arguments": tool_input,
                    "result": result,
                    "content": result,
                }
            )
            _audit["output"] = str(result)[:200]
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
            _audit["status"] = "error"
            _audit["error"] = str(exc)
            _audit["output"] = error_result
        self._end_tool_audit_span(_audit_span_id, tool_name, _audit)

    def _end_tool_audit_span(
        self,
        span_id: str | None,
        tool_name: str,
        audit: dict[str, Any],
    ) -> None:
        """P0-3: 闭合工具审计 Span（_execute_tool_use 各返回路径统一调用）。"""
        if self._trace_ctx is None or span_id is None:
            return
        self._trace_ctx.end_span(
            span_id,
            name=f"tool:{tool_name}",
            output_data=str(audit.get("output", ""))[:200],
            metadata={
                "tool_status": audit.get("status", "success"),
                "error": audit.get("error"),
            },
        )

    def _serialize_state_for_snapshot(self, state: AgentState) -> dict[str, Any]:
        """将 AgentState 序列化为 JSONB 兼容的快照字典 — 审批恢复时使用。

        P1-4: 审批创建时存储 AgentState 快照，用户批准后可从快照恢复
        Agent Loop 继续执行（而非从头开始）。

        快照内容：query / messages / retrieved_docs / tool_results /
        iteration / max_iterations / kb_ids / memory_context / tenant_id。
        """
        try:
            return {
                "query": state.get("query", ""),
                "user_id": state.get("user_id", ""),
                "session_id": state.get("session_id", ""),
                "messages": _safe_serialize(state.get("messages", [])),
                "retrieved_docs": _safe_serialize(state.get("retrieved_docs", [])),
                "tool_results": _safe_serialize(state.get("tool_results", [])),
                "answer": state.get("answer", ""),
                "iteration": state.get("iteration", 0),
                "max_iterations": state.get("max_iterations", 5),
                "kb_ids": state.get("kb_ids"),
                "memory_context": state.get("memory_context", ""),
                "tenant_id": state.get("tenant_id"),
            }
        except Exception as exc:
            log.warning("engine.snapshot.serialize_failed", error=str(exc))
            return {
                "query": state.get("query", ""),
                "session_id": state.get("session_id", ""),
                "iteration": state.get("iteration", 0),
            }

    # ------------------------------------------------------------------
    # reflect：自我反思
    # ------------------------------------------------------------------

    @trace_node("reflect")
    async def _reflect(self, state: AgentState) -> Any | None:
        """自我反思 — 评估答案质量，执行幻觉防护多层拦截。

        幻觉防护流水线（按执行顺序）：
            1. 忠实度拦截：调用 QualityGuard.check_and_regenerate，
               faithfulness 低于阈值时使用增强 prompt 重生成答案；
            2. 引用强制校验：验证答案包含 [n] 引用标注（_check_citations）；
            3. 矛盾检测：调用 ContradictionDetector.check_answer_consistency，
               检测答案与知识库文档矛盾（_check_contradiction）；
            4. 高风险核验：调用 HighRiskDetector.verify_against_sources，
               核验金额/日期/法律条款一致性（_check_high_risk）。

        降级：LLMJudgeService 不可用时走原有内联 prompt（_reflect_inline），
        仅记录日志，不阻断流程。
        """
        answer = state.get("answer", "")
        if not answer:
            return None

        retrieved_docs = state.get("retrieved_docs", [])
        contexts = [
            doc.get("content", "")
            for doc in retrieved_docs
            if doc.get("content")
        ]

        # 优先：调用 LLMJudgeService 结构化评测 + 忠实度拦截
        if self._quality_guard is not None:
            # 忠实度拦截：低置信度时重生成而非仅标记
            final_answer, eval_result = await self._quality_guard.check_and_regenerate(
                query=state["query"],
                answer=answer,
                contexts=contexts,
                generator=self.generator,
            )
            # 如果答案被重生成，更新 state
            if final_answer != answer:
                log.info(
                    "engine.reflect.answer_regenerated",
                    original_len=len(answer),
                    new_len=len(final_answer),
                )
                state["answer"] = final_answer
                state["answer_regenerated"] = True
                answer = final_answer

            if eval_result is not None:
                state["low_confidence"] = self._quality_guard.is_low_confidence(
                    eval_result
                )
                state["eval_result"] = eval_result

        # --- 幻觉防护：引用强制校验 ---
        self._check_citations(state, answer, retrieved_docs)

        # --- P1-3: Claim 内容级回溯核验 ---
        # 引用存在 ≠ 引用支持答案。核验每个 [n] 引用的 chunk 内容是否
        # 实际包含答案中声称的关键实体（金额/日期/百分比等）。
        if self._quality_guard is not None and retrieved_docs:
            try:
                claim_result = (
                    self._quality_guard.verify_claims_against_chunks(
                        answer, retrieved_docs
                    )
                )
                if claim_result["should_flag"]:
                    state["claim_unverified"] = True
                    state["claim_verification"] = claim_result
                    log.warning(
                        "engine.claim_verification_failed",
                        total=claim_result["total_claims"],
                        unverified=len(claim_result["unverified_claims"]),
                        ratio=claim_result["unverified_ratio"],
                    )
            except Exception as exc:
                log.warning("engine.claim_verification_error", error=str(exc))

        # --- P1-2: 证据不足拒答门禁 ---
        # 重排后仍低于阈值 → 不让模型继续生成，直接拒答或请求澄清
        if state.get("retrieval_insufficient"):
            log.warning(
                "engine.reflect.retrieval_insufficient",
                action="refuse_or_clarify",
            )
            refuse_answer = (
                "抱歉，当前知识库中未找到与您问题高度相关的资料。\n"
                "请尝试以下方式：\n"
                "1. 重新表述问题，提供更多细节；\n"
                "2. 确认相关文档已上传并发布到知识库；\n"
                "3. 联系知识库管理员确认文档覆盖范围。"
            )
            state["answer"] = refuse_answer
            state["low_confidence"] = True
            state["retrieval_refused"] = True
            return state.get("eval_result")

        # --- 幻觉防护：矛盾检测（check_answer_consistency 接线）---
        await self._check_contradiction(state, answer, retrieved_docs)

        # --- 幻觉防护：高风险信息二次核验 ---
        self._check_high_risk(state, answer, retrieved_docs)

        # 如果质量守卫不可用，降级到内联反思
        if self._quality_guard is None:
            await self._reflect_inline(state)

        return state.get("eval_result")

    def _check_citations(
        self,
        state: AgentState,
        answer: str,
        retrieved_docs: list[dict[str, Any]],
    ) -> None:
        """引用强制校验 — 检查答案是否包含 [n] 引用标注。

        无引用标注时标记 citation_invalid=True，供 SSE 事件和拦截使用。
        """
        if not retrieved_docs:
            return  # 无检索文档时跳过（非 RAG 场景）

        try:
            from app.rag.citation import CitationExtractor
            extractor = self._get_citation_extractor()
            result = extractor.validate_citations(answer, retrieved_docs)
            state["citation_validation"] = result.to_dict()
            if not result.valid:
                log.warning(
                    "engine.citation_invalid",
                    reason=result.reason,
                    source_count=result.source_count,
                )
                state["citation_invalid"] = True
        except Exception as exc:
            log.warning("engine.citation_check_error", error=str(exc))

    def _get_citation_extractor(self) -> Any:
        """获取或创建 CitationExtractor 实例（懒初始化）。"""
        if not hasattr(self, "_citation_extractor") or self._citation_extractor is None:
            from app.rag.citation import CitationExtractor
            self._citation_extractor = CitationExtractor()
        return self._citation_extractor

    async def _check_contradiction(
        self,
        state: AgentState,
        answer: str,
        retrieved_docs: list[dict[str, Any]],
    ) -> None:
        """矛盾检测 — 检测答案与知识库文档是否矛盾。

        check_answer_consistency 接线：将 ContradictionDetector 接入主流程。
        检测到矛盾且 action="block" 时标记 contradiction_blocked=True。
        """
        if self._contradiction_detector is None:
            return

        if not retrieved_docs or not answer:
            return

        try:
            result = await self._contradiction_detector.check_answer_consistency(
                answer=answer,
                retrieved_docs=retrieved_docs,
            )
            state["contradiction_result"] = result.to_dict()
            if result.has_contradiction and result.action == "block":
                log.error(
                    "engine.contradiction_blocked",
                    description=result.description,
                    severity=result.severity,
                    sources=result.conflicting_sources,
                )
                state["contradiction_blocked"] = True
                state["low_confidence"] = True
            elif result.has_contradiction:
                log.warning(
                    "engine.contradiction_detected",
                    description=result.description,
                    action=result.action,
                )
        except Exception as exc:
            log.warning("engine.contradiction_check_error", error=str(exc))

    def _check_high_risk(
        self,
        state: AgentState,
        answer: str,
        retrieved_docs: list[dict[str, Any]],
    ) -> None:
        """高风险信息二次核验 — 金额/日期/法律条款一致性检查。

        检测答案中的高风险信息，与来源文档核对一致性。
        未核验比例过高时标记 high_risk_blocked=True。
        """
        if self._high_risk_detector is None:
            return

        if not answer:
            return

        try:
            result = self._high_risk_detector.verify_against_sources(
                answer=answer,
                sources=retrieved_docs,
            )
            state["high_risk_result"] = result.to_dict()
            if result.action == "block":
                log.warning(
                    "engine.high_risk_blocked",
                    total=result.total_count,
                    unverified=result.unverified_count,
                    action=result.action,
                    max_risk_level=result.max_risk_level,
                )
                state["high_risk_blocked"] = True
                state["low_confidence"] = True
                # P1-8: block 决策落审计表（fire-and-forget，失败不影响主流程）
                self._schedule_high_risk_audit(state, answer, result)
            elif result.action == "confirm":
                # P1-8: 中风险（金额偏差 1-10%）— 提示核实，不阻断
                log.warning(
                    "engine.high_risk_confirm",
                    total=result.total_count,
                    unverified=result.unverified_count,
                    action=result.action,
                    max_risk_level=result.max_risk_level,
                )
                state["high_risk_confirm"] = True
            elif result.has_risk:
                log.info(
                    "engine.high_risk_warning",
                    total=result.total_count,
                    unverified=result.unverified_count,
                    action=result.action,
                )
        except Exception as exc:
            log.warning("engine.high_risk_check_error", error=str(exc))

    def _schedule_high_risk_audit(
        self,
        state: AgentState,
        answer: str,
        result: Any,
    ) -> None:
        """P1-8: block 决策落审计表 — fire-and-forget 异步写库。

        审计失败仅记录日志，不影响问答主流程；无运行中事件循环时跳过
        （同步测试场景）。
        """

        def _on_done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception():
                log.warning(
                    "engine.high_risk_audit_task_error",
                    error=str(task.exception()),
                )

        try:
            from app.services.high_risk_audit_service import (
                get_high_risk_audit_service,
            )

            coro = get_high_risk_audit_service().record_block_audit(
                query=state.get("query", ""),
                answer=answer,
                session_id=state.get("session_id", ""),
                user_id=state.get("user_id"),
                tenant_id=state.get("tenant_id"),
                total_count=result.total_count,
                unverified_count=result.unverified_count,
                max_risk_level=result.max_risk_level,
                items=[i.to_dict() for i in result.items],
            )
            try:
                task = asyncio.create_task(coro)
            except RuntimeError:
                coro.close()  # 无运行中事件循环 — 关闭协程避免泄漏告警
                log.debug(
                    "engine.high_risk_audit_skipped", reason="no running event loop"
                )
                return
            task.add_done_callback(_on_done)
            # 持有强引用防止任务被 GC 提前回收，完成后自动从集合移除
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except Exception as exc:
            log.warning("engine.high_risk_audit_schedule_error", error=str(exc))

    async def _reflect_inline(self, state: AgentState) -> None:
        """内联简单反思 — LLMJudgeService 不可用时的降级路径。

        仅记录日志，不返回结构化数据，不触发重试。
        """
        answer = state.get("answer", "")
        answer_summary = self._summarize_for_reflect(answer)

        prompt = (
            "评估以下回答摘要的质量：\n"
            "1. 是否有引用来源？\n"
            "2. 是否完整回答了用户问题？\n"
            "3. 是否有幻觉风险？\n"
            "简要回答（satisfied / needs_improvement）并说明原因。"
        )
        messages: list[Message] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"问题: {state['query']}\n\n答案摘要: {answer_summary}"},
        ]
        try:
            text = ""
            async for chunk in self.llm.chat(messages, stream=False):
                if isinstance(chunk, str):
                    text += chunk
            log.info(
                "engine.reflect_inline",
                iteration=state["iteration"],
                conclusion=text[:200],
                answer_tokens_saved=len(answer) - len(answer_summary),
                session_id=state["session_id"],
            )
        except Exception as exc:
            log.warning("engine.reflect_inline_error", error=str(exc))

    @staticmethod
    def _summarize_for_reflect(answer: str, max_chars: int = 700) -> str:
        """为 reflect 生成答案摘要 — 保留结构要点，省略详细内容。

        P1-Opt4: 提取答案的前 3 个要点行 + 首段引言，截断到 max_chars。
        要点行识别：以 - / • / * / # / 数字编号开头的行。

        Args:
            answer: 完整答案文本。
            max_chars: 摘要最大字符数（默认 700，约 200 tokens）。

        Returns:
            答案摘要文本。
        """
        if not answer:
            return ""
        lines = answer.split("\n")
        # 提取结构化要点行
        key_points = [
            line.strip()
            for line in lines
            if line.strip() and line.strip()[0] in ("-•*#") or
               (len(line.strip()) > 1 and line.strip()[0].isdigit() and
                line.strip()[1] in ".、)")
        ]
        intro = lines[0].strip() if lines else ""
        if key_points:
            summary = f"{intro}\n" + "\n".join(key_points[:3])
        else:
            summary = intro
        return summary[:max_chars]

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
