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

from app.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: LangFuse 客户端单例（延迟初始化）
_langfuse_client: Any = None
#: LangFuse 是否可用
_langfuse_available: bool | None = None


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
    ) -> None:
        self.trace_name = trace_name
        self.session_id = session_id
        self.user_id = user_id
        self.metadata = metadata or {}
        self._trace: Any = None
        self._spans: list[Any] = []

    def start(self) -> None:
        """启动 Trace。"""
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

    def span(
        self,
        name: str,
        input_data: Any = None,
        output_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """记录一个节点 Span。

        Args:
            name: 节点名称（think / retrieve / generate / reflect）。
            input_data: 节点输入。
            output_data: 节点输出。
            metadata: 额外元数据（token_count, doc_count 等）。
        """
        if self._trace is None:
            return

        try:
            span = self._trace.span(
                name=name,
                input=input_data,
                output=output_data,
                metadata=metadata or {},
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
        if self._trace is None:
            return

        try:
            self._trace.update(
                output=output,
                metadata={
                    **self.metadata,
                    **(metadata or {}),
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
    - 节点名称
    - 输入状态摘要
    - 输出结果摘要
    - 执行延迟（ms）
    - 异常（如有）

    LangFuse 不可用时降级为纯日志，不影响业务逻辑。
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            start_time = time.perf_counter()
            error: str | None = None
            result: T

            try:
                result = await func(self, *args, **kwargs)
                return result
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

                # 从 state 中提取上下文（AgenticRAGEngine 的节点都有 state 参数）
                state = args[0] if args and isinstance(args[0], dict) else None
                session_id = state.get("session_id", "") if state else ""
                iteration = state.get("iteration", 0) if state else 0

                # 记录到 LangFuse（如果有 trace 上下文）
                trace_ctx: TraceContext | None = getattr(
                    self, "_trace_ctx", None
                )
                if trace_ctx is not None:
                    trace_ctx.span(
                        name=f"{node_name}_iter{iteration}",
                        input_data={
                            "iteration": iteration,
                            "session_id": session_id,
                        },
                        output_data=(
                            {"error": error} if error else
                            {"result_preview": str(result)[:200]}
                        ),
                        metadata={
                            "latency_ms": latency_ms,
                            "error": error,
                            "retrieved_docs": len(state.get("retrieved_docs", [])) if state else 0,
                            "tool_results": len(state.get("tool_results", [])) if state else 0,
                        },
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
