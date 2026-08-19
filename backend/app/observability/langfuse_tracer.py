"""
LangFuse 全链路追踪 — 对 Agent Loop 每个节点进行 Trace 级追踪。

架构：
    AgenticRAGEngine.answer()
        ↓ @trace_node("think")
        ↓ @trace_node("retrieve")
        ↓ @trace_node("generate")
        ↓ @trace_node("reflect")
        ↓
    LangFuseTracer.record(node, input, output, latency, tokens)
        ↓
    LangFuse SDK → LangFuse Server（SaaS 或自托管）

设计要点：
    - LangFuse 未安装/未配置时静默降级为纯日志，不影响主流程
    - 每个节点记录：node_name / input / output / latency_ms / token_count / metadata
    - 支持链式 Trace（一个问答请求 = 一个 Trace，每个节点 = 一个 Span）
    - Trace ID 与 session_id 关联，支持多轮对话链路追踪
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.eval.span_types import SpanType
from app.observability.pii_scrubber import PIIScrubber, get_default_scrubber
from app.observability.span_sampler import SpanSampler, get_default_sampler
from app.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: LangFuse 客户端单例（延迟初始化）
_langfuse_client: Any = None
#: LangFuse 是否可用
_langfuse_available: bool | None = None


def _get_pii_scrubber() -> PIIScrubber:
    """获取 PII 脱敏器单例（从 config 读取开关）。

    模块级辅助函数，便于测试 patch 替换为 mock scrubber。
    LangFuse 不可用时仍会调用 — 本地 SpanRecord 双写同样需要脱敏。
    """
    return get_default_scrubber()


def _get_span_sampler() -> SpanSampler:
    """获取 span 采样器单例（从 config 读取策略）。

    模块级辅助函数，便于测试 patch 替换为 mock sampler。
    仅作用于 LangFuse 双写分支，本地 SpanRecord 不采样（评测需完整数据）。
    """
    return get_default_sampler()


async def _append_event_log(
    session_id: str,
    node_name: str,
    iteration: int,
    error: str | None,
    latency_ms: float,
    state: Any,
    result: Any,
) -> None:
    """P2-7: 追加 Agent Loop 节点事件日志。

    通过 contextvar 拾取请求级 EventLogManager，未注入时跳过（零开销）。
    失败不抛异常 — 事件日志是 Checkpoint 的补充，不应阻塞主流程。

    output_data 语义：
    - 节点返回 dict（LangGraph 标准模式）：作为 state 增量，可重放
    - 节点返回非 dict（str/None 等）：仅保存 result_preview，不可重放（审计用）

    Args:
        session_id: 会话 ID（空字符串时跳过）。
        node_name: 节点名（think / retrieve / tool_call / generate / reflect）。
        iteration: Agent Loop 迭代轮次。
        error: 异常信息（None 表示成功）。
        latency_ms: 节点执行延迟（毫秒）。
        state: Agent 状态（用于提取证据计数，不直接存入事件）。
        result: 节点返回值（dict 时作为 state 增量）。
    """
    if not session_id:
        return
    try:
        from app.memory.event_log import get_current_event_log

        manager = get_current_event_log()
        if manager is None:
            return

        # 构造 output_data — dict 返回值作为 state 增量（可重放）
        if isinstance(result, dict):
            output_data = result
        elif error:
            output_data = {"error": error}
        else:
            # 非 dict 返回值 — 仅审计用，不参与重放
            output_data = {"result_preview": str(result)[:200]} if result is not None else None

        # metadata — 节点执行的额外证据
        meta = {
            "latency_ms": latency_ms,
            "retrieved_docs": len(state.get("retrieved_docs", [])) if state else 0,
            "tool_results": len(state.get("tool_results", [])) if state else 0,
        }
        if error:
            meta["error"] = error

        await manager.append(
            session_id=session_id,
            event_type="node_end",
            node_name=node_name,
            output_data=output_data,
            iteration=iteration,
            metadata=meta,
        )
    except Exception as exc:
        # 事件日志失败不影响主流程 — Checkpoint 是兜底恢复机制
        logger.warning(
            "event_log.append_failed_in_tracer",
            session_id=session_id,
            node_name=node_name,
            error=str(exc),
        )

#: Agent Loop 节点名 → 标准 SpanType 映射（P0-1：消灭 SpanType 死枚举，
#: 使 persist_tool_spans 的过滤集合能匹配到 tool.call 等标准类型）。
#: 未映射的节点名原样作为 span_type 使用（向后兼容）。
NODE_SPAN_TYPES: dict[str, str] = {
    "think": SpanType.PLAN_CREATE.value,
    "retrieve": SpanType.CONTEXT_LOAD.value,
    "tool_call": SpanType.TOOL_CALL.value,
    "generate": SpanType.STATE_UPDATE.value,
    "reflect": SpanType.SCORE_COMPUTE.value,
}

#: task run 根 Span 类型（树形 Trace 的根节点）
TASK_RUN_SPAN_TYPE: str = "task.run"


def map_span_type(node_name: str) -> str:
    """将 Agent Loop 节点名映射为标准 SpanType 值。"""
    return NODE_SPAN_TYPES.get(node_name, node_name)


def _get_langfuse_client() -> Any:
    """延迟获取 LangFuse 客户端。

    Returns:
        LangFuse 客户端实例，或 None（未安装/未配置）。
    """
    global _langfuse_client, _langfuse_available

    if _langfuse_available is False:
        return None

    if _langfuse_client is not None:
        return _langfuse_client

    try:
        from langfuse import Langfuse

        from app.config import get_settings

        settings = get_settings()

        # 检查配置是否完整
        public_key = getattr(settings, "LANGFUSE_PUBLIC_KEY", "")
        secret_key = getattr(settings, "LANGFUSE_SECRET_KEY", "")
        host = getattr(settings, "LANGFUSE_HOST", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            _langfuse_available = False
            logger.info("langfuse.not_configured")
            return None

        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        _langfuse_available = True
        logger.info("langfuse.connected", host=host)
    except ImportError:
        _langfuse_available = False
        logger.info("langfuse.not_installed")
    except Exception as exc:
        _langfuse_available = False
        logger.warning("langfuse.init_error", error=str(exc))

    return _langfuse_client


def flush_langfuse() -> None:
    """Flush 所有待发送的 LangFuse 事件到服务端。

    在应用关闭时调用，确保追踪数据不丢失。
    LangFuse 不可用时静默跳过。
    """
    client = _get_langfuse_client()
    if client is None:
        return
    try:
        client.flush()
        logger.info("langfuse.flushed")
    except Exception as exc:
        logger.warning("langfuse.flush_error", error=str(exc))


class TraceContext:
    """单次问答请求的 Trace 上下文。

    一个 TraceContext 对应一次 Agent Loop 执行，
    包含一个 LangFuse trace 和多个 span（每个节点一个）。
    """

    def __init__(
        self,
        trace_name: str = "rag_agent_loop",
        session_id: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
        recorder: Any | None = None,
    ) -> None:
        self.trace_name = trace_name
        self.session_id = session_id
        self.user_id = user_id
        self.metadata = metadata or {}
        self._trace: Any = None
        self._spans: list[Any] = []
        # task run 根 Span ID（recorder 压栈，使节点 Span 形成树而非扁平序列）
        self._root_span_id: str | None = None
        # P3: span_id → evidence_ref 暂存（start_span 记录、end_span 消费），
        # 避免把内部键 _evidence_ref 泄漏进 recorder 的公开 metadata
        self._evidence_stash: dict[str, str] = {}
        # 标准 Span 收集器（双写目标）— 显式传入或从 contextvar 拾取，
        # 使 EvalRunner 注入的 recorder 对 engine 零改动生效
        if recorder is None:
            try:
                from app.observability.span_record import get_current_recorder

                recorder = get_current_recorder()
            except Exception:  # pragma: no cover - 防御性降级
                recorder = None
        self.recorder = recorder

    def start(self) -> None:
        """启动 Trace。"""
        # 本地根 Span：节点 Span 的父节点（P0-4 树形 Trace）
        self._ensure_root_span()

        client = _get_langfuse_client()
        if client is None:
            return

        try:
            self._trace = client.trace(
                name=self.trace_name,
                session_id=self.session_id or None,
                user_id=self.user_id or None,
                metadata=self.metadata,
            )
        except Exception as exc:
            logger.warning("langfuse.trace_start_error", error=str(exc))
            self._trace = None

    def _ensure_root_span(self) -> None:
        """确保 task run 根 Span 已压栈（幂等）。

        所有通过 start_span / span 记录的节点 Span 都以此根为祖先，
        解决"所有 parent_span_id=None、Trace 是扁平序列"的结构性问题。
        """
        if self.recorder is None or self._root_span_id is not None:
            return
        try:
            self._root_span_id = self.recorder.start_span(
                span_type=TASK_RUN_SPAN_TYPE,
                name=self.trace_name,
                metadata={
                    "session_id": self.session_id,
                    "user_id": self.user_id,
                    **{k: v for k, v in self.metadata.items()
                       if isinstance(v, (str, int, float, bool))},
                },
            )
        except Exception as exc:  # pragma: no cover - 防御性降级
            logger.warning("span_record.root_span_error", error=str(exc))
            self._root_span_id = None

    def start_span(
        self,
        name: str,
        span_type: str | None = None,
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
        evidence_ref: str | None = None,
    ) -> str | None:
        """开始一个节点 Span（压栈，with 块/try-finally 内配对 end_span）。

        与 ``span()``（事后一次性记录）不同，本方法将 Span 压入栈，
        其间记录的子 Span（如 retrieve 内的 permission.decision、
        think 前的 compaction_event）自动以本 Span 为父节点。

        evidence_ref 暂存于内部 stash，由对应的 end_span 取出写入
        SpanRecord.evidence_ref（P3 证据贯穿）。

        Returns:
            span_id；recorder 不可用时返回 None（调用方需容忍）。
        """
        if self.recorder is None:
            return None
        self._ensure_root_span()
        try:
            span_id = self.recorder.start_span(
                span_type=span_type or name,
                name=name,
                input_ref=str(input_data)[:500] if input_data is not None else None,
                metadata=dict(metadata or {}),
            )
            # P3: evidence_ref 暂存于 stash（span_id → ref），end_span 消费
            if evidence_ref is not None and span_id is not None:
                self._evidence_stash[span_id] = evidence_ref
            return span_id
        except Exception as exc:  # pragma: no cover - 防御性降级
            logger.warning("span_record.start_error", name=name, error=str(exc))
            return None

    def end_span(
        self,
        span_id: str | None,
        name: str,
        output_data: Any = None,
        metadata: dict[str, Any] | None = None,
        langfuse_input: Any = None,
        evidence_ref: str | None = None,
    ) -> None:
        """结束 start_span 开启的节点 Span，并双写 LangFuse。

        Args:
            span_id: start_span 返回的 ID（None 时跳过本地记录）。
            name: 节点名称（LangFuse 侧展示用）。
            output_data: 节点输出摘要。
            metadata: 结束时可得的元数据（latency_ms / error / 证据键）。
            langfuse_input: LangFuse 侧展示的输入（与本地 input_ref 分离）。
            evidence_ref: 证据引用（doc_id / artifact ID 等）；None 时
                回退到 start_span 暂存的 stash 值（P3 证据贯穿）。
        """
        meta = dict(metadata or {})
        error = meta.get("error")
        # P3: 结束时的显式证据优先；缺省时取 start_span 暂存的 stash 并弹出
        if evidence_ref is None and span_id is not None:
            evidence_ref = self._evidence_stash.pop(span_id, None)

        # P1-6: PII 脱敏 — span export 前对 input/output/metadata 递归 scrub
        # 本地 SpanRecord 和 LangFuse 双写使用相同的脱敏后数据，
        # 避免本地观测数据比 LangFuse 侧更宽松（PII 不应出现在任何观测面）。
        scrubber = _get_pii_scrubber()
        scrub_input, scrub_output, scrub_meta = scrubber.scrub_span_io(
            langfuse_input, output_data, meta
        )

        # 本地标准 SpanRecord 闭合
        if self.recorder is not None and span_id is not None:
            try:
                cost = {
                    k: scrub_meta[k] for k in ("latency_ms", "token_count") if k in scrub_meta
                }
                self.recorder.end_span(
                    span_id,
                    status="error" if error else "ok",
                    output_ref=str(scrub_output)[:500] if scrub_output is not None else None,
                    error=str(error) if error else None,
                    cost=cost,
                    evidence_ref=str(evidence_ref)[:500] if evidence_ref is not None else None,
                    metadata=scrub_meta,
                )
            except Exception as exc:  # pragma: no cover - 防御性降级
                logger.warning("span_record.end_error", name=name, error=str(exc))

        # LangFuse 双写
        if self._trace is not None:
            # P2-9: span 采样 — 仅作用于 LangFuse 侧，本地 SpanRecord 不采样
            # error span 强制上报（故障证据），正常 span 按采样率随机
            error_flag = bool(scrub_meta.get("error"))
            if _get_span_sampler().should_sample(metadata=scrub_meta, is_error=error_flag):
                try:
                    span = self._trace.span(
                        name=name,
                        input=scrub_input,
                        output=scrub_output,
                        metadata=scrub_meta,
                    )
                    self._spans.append(span)
                except Exception as exc:
                    logger.warning("langfuse.span_error", name=name, error=str(exc))

    def span(
        self,
        name: str,
        input_data: Any = None,
        output_data: Any = None,
        metadata: dict[str, Any] | None = None,
        span_type: str | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        """记录一个已完成的节点 Span（事后闭合记录）。

        父节点取当前栈顶（根 Span 或正在执行的节点 Span），
        适用于无法 start/end 两段式的内联埋点（如 generate 流式生成）。

        Args:
            name: 节点名称（think / retrieve / generate / reflect）。
            input_data: 节点输入。
            output_data: 节点输出。
            metadata: 额外元数据（token_count, doc_count 等）。
            span_type: 标准 SpanType 值（缺省取 name，P0-1 对齐）。
        """
        # P1-6: PII 脱敏 — span export 前递归 scrub input/output/metadata
        scrubber = _get_pii_scrubber()
        scrub_input, scrub_output, scrub_meta = scrubber.scrub_span_io(
            input_data, output_data, metadata
        )

        # 双写分支 1：本地标准 SpanRecord（评测消费，不依赖 LangFuse）
        if self.recorder is not None:
            self._ensure_root_span()
            try:
                error = scrub_meta.get("error")
                cost = {
                    k: scrub_meta[k] for k in ("latency_ms", "token_count") if k in scrub_meta
                }
                self.recorder.record_closed(
                    name=name,
                    span_type=span_type or name,
                    input_ref=str(scrub_input)[:500] if scrub_input is not None else None,
                    output_ref=str(scrub_output)[:500] if scrub_output is not None else None,
                    error=str(error) if error else None,
                    cost=cost,
                    evidence_ref=str(evidence_ref)[:500] if evidence_ref is not None else None,
                    metadata=scrub_meta,
                )
            except Exception as exc:  # pragma: no cover - 防御性降级
                logger.warning("span_record.write_error", name=name, error=str(exc))

        # 双写分支 2：LangFuse（实时观测，可选）
        if self._trace is None:
            return

        # P2-9: span 采样 — error span 强制上报，正常 span 按采样率随机
        error_flag = bool(scrub_meta.get("error"))
        if not _get_span_sampler().should_sample(metadata=scrub_meta, is_error=error_flag):
            return

        try:
            span = self._trace.span(
                name=name,
                input=scrub_input,
                output=scrub_output,
                metadata=scrub_meta,
            )
            self._spans.append(span)
        except Exception as exc:
            logger.warning("langfuse.span_error", name=name, error=str(exc))

    def finalize(
        self,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """结束 Trace，记录最终输出和汇总元数据。"""
        # P1-6: PII 脱敏 — Trace 收尾的 output/metadata 同样含业务内容
        # scrub_span_io 返回 (input, output, metadata)，finalize 无 input，首位置丢弃
        scrubber = _get_pii_scrubber()
        _, scrub_output, scrub_meta = scrubber.scrub_span_io(None, output, metadata)

        # 闭合本地根 Span（失败标记 error 由 collect() 兜底为 timeout 语义）
        if self.recorder is not None and self._root_span_id is not None:
            try:
                self.recorder.end_span(
                    self._root_span_id,
                    status="ok",
                    output_ref=str(scrub_output)[:500] if scrub_output is not None else None,
                    metadata=scrub_meta,
                )
            except Exception as exc:  # pragma: no cover - 防御性降级
                logger.warning("span_record.finalize_error", error=str(exc))
            finally:
                self._root_span_id = None

        if self._trace is None:
            return

        try:
            # 合并时 scrub self.metadata（Trace 启动时传入的可能含 PII，
            # 例如 user_id 误填邮箱的场景），保证最终上报的 metadata 全部脱敏
            scrubbed_trace_meta = scrubber.scrub_value(self.metadata)
            if not isinstance(scrubbed_trace_meta, dict):
                scrubbed_trace_meta = {}
            self._trace.update(
                output=scrub_output,
                metadata={
                    **scrubbed_trace_meta,
                    **scrub_meta,
                    "span_count": len(self._spans),
                },
            )
        except Exception as exc:
            logger.warning("langfuse.finalize_error", error=str(exc))


def trace_node(node_name: str) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """装饰器：追踪 Agent Loop 节点的执行。

    用法：

    .. code-block:: python

        class AgenticRAGEngine:
            @trace_node("think")
            async def _think(self, state: AgentState) -> str:
                ...

    记录内容：
    - 节点名称与标准 span_type（NODE_SPAN_TYPES 映射，P0-1）
    - 输入状态摘要
    - 输出结果摘要
    - 执行延迟（ms）
    - 异常（如有）
    - state["_span_evidence"]：节点执行期间产出的证据元数据
      （included_refs / trust_levels / raw_decision_text 等，P0-2/P1-3），
      在 Span 闭合时合并进 metadata 并由节点负责消费清理。

    Span 通过 start_span/end_span 压栈闭合，节点执行期间记录的
    子 Span（permission.decision / compaction_event 等）自动挂为子节点，
    形成树形 Trace（P0-4）。

    LangFuse 不可用时降级为纯日志，不影响业务逻辑。
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            start_time = time.perf_counter()
            error: str | None = None
            # 显式初始化为 None — 异常路径下 func() 未完成赋值，
            # finally 块仍会引用 result（_append_event_log / result_preview），
            # 未初始化会抛 UnboundLocalError 掩盖真实异常。
            result: T | None = None

            # 从 state 中提取上下文（AgenticRAGEngine 的节点都有 state 参数）
            state = args[0] if args and isinstance(args[0], dict) else None
            session_id = state.get("session_id", "") if state else ""
            iteration = state.get("iteration", 0) if state else 0

            # 压栈开启节点 Span（recorder 不可用时 span_id=None，零开销）
            trace_ctx: TraceContext | None = getattr(
                self, "_trace_ctx", None
            )
            span_name = f"{node_name}_iter{iteration}"
            span_id: str | None = None
            if trace_ctx is not None:
                span_id = trace_ctx.start_span(
                    name=span_name,
                    span_type=map_span_type(node_name),
                    input_data={
                        "iteration": iteration,
                        "session_id": session_id,
                    },
                )

            try:
                result = await func(self, *args, **kwargs)
                return result
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

                if trace_ctx is not None:
                    # 节点执行期间写入的证据（P0-2/P1-3）合并进 Span metadata
                    evidence: dict[str, Any] = {}
                    if state is not None:
                        raw_evidence = state.pop("_span_evidence", None)
                        if isinstance(raw_evidence, dict):
                            evidence = raw_evidence
                    # P3: 节点证据（若带 evidence_ref）单独透传，不混进 metadata
                    node_evidence_ref = evidence.get("evidence_ref")
                    trace_ctx.end_span(
                        span_id,
                        name=span_name,
                        output_data=(
                            {"error": error} if error else
                            {"result_preview": str(result)[:200]}
                        ),
                        metadata={
                            "latency_ms": latency_ms,
                            "error": error,
                            "retrieved_docs": len(state.get("retrieved_docs", [])) if state else 0,
                            "tool_results": len(state.get("tool_results", [])) if state else 0,
                            **{
                                k: v for k, v in evidence.items()
                                if k != "evidence_ref"
                            },
                        },
                        evidence_ref=str(node_evidence_ref)[:500] if node_evidence_ref is not None else None,
                        langfuse_input={
                            "iteration": iteration,
                            "session_id": session_id,
                        },
                    )

                # P2-7: 追加事件日志 — 与 Checkpoint 互补的混合恢复机制
                # 通过 contextvar 拾取请求级 EventLogManager，未注入时跳过（零开销）
                await _append_event_log(
                    session_id=session_id,
                    node_name=node_name,
                    iteration=iteration,
                    error=error,
                    latency_ms=latency_ms,
                    state=state,
                    result=result,
                )

                # 无论 LangFuse 是否可用，都记录日志
                logger.info(
                    f"agent_loop.{node_name}",
                    latency_ms=latency_ms,
                    iteration=iteration,
                    session_id=session_id,
                    error=error,
                )

        return wrapper

    return decorator
