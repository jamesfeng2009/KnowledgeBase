"""
Agent 异常层级 —— 所有运行时自产异常的统一基类。

设计动机：
    LLM 是概率系统，生产环境需要把"终止信号"做成异常 ——
    预算耗尽、死循环检测、工具越权 等。
    所有运行时自产异常共享 AgentError 基类，携带结构化 context，
    便于日志聚合 / 告警归类 / 错误分类器（error_classifier）识别。

与 error_classifier 的关系：
    BudgetExceeded / InfiniteLoopDetected 等异常会被 classify_error 识别为
    不可重试的运行时信号 —— 下游 retry 不应重试这些异常。

遵循单一职责：本模块仅定义异常类型，不含处理逻辑。
遵循开闭原则：新增运行时异常在此追加，error_classifier 自动支持。
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """所有 Agent 运行时自产异常的基类。

    携带结构化 context —— 任意关键字参数自动入 context dict，
    便于日志聚合 / 告警归类 / event log 序列化。

    子类只需覆盖 __init__ 设置默认 message，调用 super().__init__(message, **context)。
    """

    def __init__(self, message: str, **context: Any) -> None:
        self.context: dict[str, Any] = dict(context)
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict，便于写入 event log / 告警系统。"""
        return {
            "error_type": self.__class__.__name__,
            "message": str(self),
            "context": self.context,
        }


class BudgetExceeded(AgentError):
    """预算耗尽 —— 四轴硬上限任一触顶。

    运行时自产终止信号，不可重试 —— 重试只会再次超预算。

    Attributes:
        axis: 触顶的预算轴（turns / seconds / tokens / cost_usd）。
        value: 当前值。
        limit: 上限值。
        run_id: 关联的 run 标识。
    """

    def __init__(
        self,
        axis: str,
        value: float,
        limit: float,
        *,
        run_id: str | None = None,
    ) -> None:
        self.axis = axis
        self.value = value
        self.limit = limit
        self.run_id = run_id
        super().__init__(
            f"Budget exceeded on axis '{axis}': {value:.2f} > {limit:.2f}",
            axis=axis,
            value=value,
            limit=limit,
            run_id=run_id,
        )


class InfiniteLoopDetected(AgentError):
    """死循环检测 —— Agent 在固定窗口内重复相同动作。

    运行时自产终止信号，不可重试 —— 重试会继续循环。

    Attributes:
        pattern: 重复的动作模式（如 "相同 tool_call 连续 3 次"）。
        window: 检测窗口大小。
    """

    def __init__(self, pattern: str, *, window: int = 10) -> None:
        self.pattern = pattern
        self.window = window
        super().__init__(
            f"Infinite loop detected: {pattern} (window={window})",
            pattern=pattern,
            window=window,
        )


# ------------------------------------------------------------------
# 安全相关异常 —— 用于 SECURITY_VETO 集中判断
# ------------------------------------------------------------------

class SecurityViolation(AgentError):
    """安全违规 —— 注入检测 / 权限拒绝 / 污点追踪拦截。"""

    pass


class PermissionDenied(AgentError):
    """权限拒绝 —— 工具调用未通过权限矩阵检查。"""

    pass


class PromptInjectionDetected(SecurityViolation):
    """Prompt 注入检测命中 —— 用户输入 / 检索结果 / 工具参数含注入模式。"""

    def __init__(self, source: str, *, pattern: str | None = None) -> None:
        self.source = source
        self.pattern = pattern
        super().__init__(
            f"Prompt injection detected from source: {source}",
            source=source,
            pattern=pattern,
        )


# ------------------------------------------------------------------
# 安全否决异常集合 —— 上层一次 isinstance 判断走安全通道
# ------------------------------------------------------------------

SECURITY_VETO_EXCEPTIONS: tuple[type[Exception], ...] = (
    SecurityViolation,
    PermissionDenied,
    PromptInjectionDetected,
)
