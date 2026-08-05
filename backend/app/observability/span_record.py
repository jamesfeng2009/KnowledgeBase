"""
标准 Span 记录与收集器 — 评测可消费的本地 Trace 事件模型。

双写架构：
    engine 节点（@trace_node 已有埋点，零改动）
            ↓
    TraceContext.span()
            ├──→ LangFuse span     「看」：实时观测，可选
            └──→ SpanRecorder      「算」：本地标准 SpanRecord，评测消费

为什么需要本地记录：
    - 评测不能依赖 LangFuse 可用性（私有部署默认不配置）；
    - LangFuse span 是异步上报、面向展示的，轨迹校验需要同步、
      结构化、带父子关系的本地记录。

父子关系维护：
    SpanRecorder 内部用栈维护，start_span 时 push、end_span 时 pop，
    新 span 的 parent_span_id 取栈顶。跨函数/跨节点传递通过
    contextvar（与 engine._trace_ctx_var 同款模式）。

运行模式：
    - 评测模式：EvalRunner 在 case 执行前 ``with span_recorder() as rec``，
      结束后 rec.collect() 得到全量 span；
    - 生产模式：不注入 recorder 时零开销（TraceContext 双写分支不执行）。
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Generator

from app.utils.logger import get_logger

log = get_logger(__name__)

#: 当前激活的 SpanRecorder（contextvar，跨 async 调用链传递）
_recorder_var: ContextVar[SpanRecorder | None] = ContextVar(
    "span_recorder", default=None
)


@dataclass
class SpanRecord:
    """标准 Span 记录 — 一次有意义操作的结构化证据。

    Attributes:
        span_id: Span 唯一 ID。
        parent_span_id: 父 Span ID（栈顶），根 Span 为 None。
        span_type: Span 类型（SpanType 值或节点名，如 think/retrieve）。
        name: 可读名称。
        start_time: 开始时间戳（秒）。
        end_time: 结束时间戳（秒），未结束为 None。
        status: ok / error / timeout。
        input_ref: 输入引用（摘要/hash/路径，不复制全文）。
        output_ref: 输出引用。
        error: 错误信息。
        cost: 成本字典（latency_ms / token 等）。
        evidence_ref: 证据引用（artifact ID / doc_id / URL）。
        metadata: 额外元数据。
    """

    span_id: str
    parent_span_id: str | None
    span_type: str
    name: str
    start_time: float
    end_time: float | None = None
    status: str = "ok"
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    cost: dict[str, Any] = field(default_factory=dict)
    evidence_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def latency_ms(self) -> float | None:
        """耗时（毫秒），未结束时返回 None。"""
        if self.end_time is None:
            return None
        return round((self.end_time - self.start_time) * 1000, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "span_type": self.span_type,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "input_ref": self.input_ref,
            "output_ref": self.output_ref,
            "error": self.error,
            "cost": self.cost,
            "evidence_ref": self.evidence_ref,
            "metadata": self.metadata,
        }


class SpanRecorder:
    """一次 task run 的标准 Span 收集器。

    使用方式::

        with span_recorder() as rec:
            ...执行 Agent Loop...
        spans = rec.collect()

    或手动管理::

        rec = SpanRecorder()
        sid = rec.start_span("tool.call", name="knowledge_search")
        rec.end_span(sid, status="ok", output_ref="返回 3 条文档")
    """

    def __init__(self) -> None:
        self._spans: list[SpanRecord] = []
        self._open: dict[str, SpanRecord] = {}
        self._stack: list[str] = []

    # ------------------------------------------------------------------
    # start / end 生命周期
    # ------------------------------------------------------------------

    def start_span(
        self,
        span_type: str,
        name: str | None = None,
        input_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """开始一个 Span，返回 span_id。parent 取当前栈顶。"""
        span_id = uuid.uuid4().hex[:16]
        record = SpanRecord(
            span_id=span_id,
            parent_span_id=self._stack[-1] if self._stack else None,
            span_type=str(span_type),
            name=name or str(span_type),
            start_time=time.time(),
            input_ref=input_ref,
            metadata=dict(metadata or {}),
        )
        self._spans.append(record)
        self._open[span_id] = record
        self._stack.append(span_id)
        return span_id

    def end_span(
        self,
        span_id: str,
        status: str = "ok",
        output_ref: str | None = None,
        error: str | None = None,
        cost: dict[str, Any] | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        """结束一个 Span。"""
        record = self._open.pop(span_id, None)
        if record is None:
            log.warning("span_record.end_unknown", span_id=span_id)
            return
        record.end_time = time.time()
        record.status = status
        record.output_ref = output_ref
        record.error = error
        record.cost = dict(cost or {})
        if record.latency_ms is not None:
            record.cost.setdefault("latency_ms", record.latency_ms)
        record.evidence_ref = evidence_ref
        # 从栈中移除（正常情况在栈顶；异常嵌套时做防御性清理）
        if span_id in self._stack:
            while self._stack and self._stack[-1] != span_id:
                self._stack.pop()
            if self._stack:
                self._stack.pop()

    @contextmanager
    def span(
        self,
        span_type: str,
        name: str | None = None,
        input_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Generator[str, None, None]:
        """上下文管理器形式的 Span — with 块结束自动关闭。"""
        span_id = self.start_span(
            span_type, name=name, input_ref=input_ref, metadata=metadata
        )
        try:
            yield span_id
        except Exception as exc:
            self.end_span(span_id, status="error", error=str(exc))
            raise
        else:
            self.end_span(span_id, status="ok")

    def record_closed(
        self,
        name: str,
        span_type: str | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
        error: str | None = None,
        cost: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """记录一个已完成的 Span（由 TraceContext 双写调用）。

        @trace_node 等已有埋点是"事后记录"模式（自带 latency 统计），
        不适合 start/end 两段式，这里一次性写入闭合 Span。
        """
        record = SpanRecord(
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=self._stack[-1] if self._stack else None,
            span_type=str(span_type or name),
            name=name,
            start_time=time.time(),
            end_time=time.time(),
            status="error" if error else "ok",
            input_ref=input_ref,
            output_ref=output_ref,
            error=error,
            cost=dict(cost or {}),
            metadata=dict(metadata or {}),
        )
        self._spans.append(record)

    # ------------------------------------------------------------------
    # 收集
    # ------------------------------------------------------------------

    def collect(self) -> list[SpanRecord]:
        """收集全部 Span（含未正常关闭的，标记为 timeout 语义由调用方判定）。"""
        for record in self._open.values():
            record.end_time = record.end_time or time.time()
            if record.status == "ok":
                record.status = "timeout"
        self._open.clear()
        self._stack.clear()
        return list(self._spans)

    def __len__(self) -> int:
        return len(self._spans)


# ======================================================================
# contextvar 集成 — 评测模式注入
# ======================================================================


def get_current_recorder() -> SpanRecorder | None:
    """获取当前激活的 SpanRecorder（未注入时返回 None）。"""
    return _recorder_var.get()


class span_recorder:
    """激活一个 SpanRecorder（contextvar 作用域）。

    评测模式：EvalRunner 在 case 执行前进入此上下文，
    TraceContext 创建时自动从 contextvar 拾取 recorder，实现 engine 零改动。

    以显式类实现替代 ``@contextmanager`` 装饰器，规避 contextlib 装饰器在
    静态检查/后续 Python 版本中的潜在变更，行为与原函数完全等价。
    """

    def __init__(self, recorder: SpanRecorder | None = None) -> None:
        # 注意不能用 `recorder or SpanRecorder()`：SpanRecorder 实现了 __len__，
        # 空 recorder 为 falsy 会被错误替换
        self.recorder = recorder if recorder is not None else SpanRecorder()
        self._token: Token[SpanRecorder | None] | None = None

    def __enter__(self) -> SpanRecorder:
        self._token = _recorder_var.set(self.recorder)
        return self.recorder

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        if self._token is not None:
            _recorder_var.reset(self._token)
            self._token = None
