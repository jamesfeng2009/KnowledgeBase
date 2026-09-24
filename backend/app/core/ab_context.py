"""
AB 实验的请求级上下文 — 单一职责：把分流结论传递给生成层与编排层。

为什么需要 contextvar：
    A/B 实验要分的不只是「用哪个模型」，还有 prompt 变体与编排参数
    （如 Agent Loop 迭代上限）。这些消费点分散在 Generator 与
    AgenticRAGEngine 内部，而两处都不希望为了实验参数改函数签名
    （engine.answer() 已有 15 个参数）。

    contextvar 是本项目既有的传递手法（span_recorder / event_log 同款）：
    请求入口 set 一次，调用链深处 get，async 并发下互不串扰。

职责边界：
    - 本模块只存/取分流结论，不做分桶计算（那是 app.core.ab_split）；
    - 消费方按需读取自己关心的字段，读不到就是默认行为 —— 未命中实验
      时 get 返回 None，线上路径零变化。

生命周期：
    chat_service 在准备阶段 ``with ab_scope(assignment):`` 包住整次对话，
    离开作用域自动复位；嵌套进入（评测复用）以外层为准不叠加。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ABAssignment",
    "ab_scope",
    "active_prompt_variant",
    "bind_ab_assignment",
    "effective_max_iterations",
    "get_ab_assignment",
    "peek_ab_metadata",
]

#: Agent Loop 迭代上限的实验硬上限 — 防止配置写错把线上打成无限循环
_MAX_ITERATIONS_CEILING = 10


@dataclass(frozen=True)
class ABAssignment:
    """一次请求命中的实验分组（未命中时各字段取默认值）。

    Attributes:
        experiment: 实验名；空串表示未命中任何实验。
        arm: control / treatment / ""（未命中）。
        bucket: sticky 分桶号 0-99；-1 表示未参与分桶（如白名单直通）。
        model: 生效模型 ID；空串表示沿用调用方解析结果。
        prompt_variant: 生效 prompt 变体名；空串表示用线上默认指引。
        max_iterations: 生效的 Agent Loop 迭代上限；None 表示沿用引擎默认。
        via: 命中路径（allowlist / traffic），供归因日志区分定向灰度与随机分流。
        correlation_id: 本次请求/决策的关联键（会话或请求 ID）。曝光日志与
            trace 都带上它，才能把分组结论与下游业务结果 join 起来。
    """

    experiment: str = ""
    arm: str = ""
    bucket: int = -1
    model: str = ""
    prompt_variant: str = ""
    max_iterations: int | None = None
    via: str = ""
    correlation_id: str = ""

    @property
    def hit(self) -> bool:
        """是否命中实验（未命中的请求不应产生归因埋点）。"""
        return bool(self.experiment) and self.arm in ("control", "treatment")


_assignment_var: ContextVar[ABAssignment | None] = ContextVar(
    "ab_assignment", default=None
)


def get_ab_assignment() -> ABAssignment | None:
    """取当前请求的分流结论（未设置时返回 None）。"""
    return _assignment_var.get()


@contextmanager
def ab_scope(assignment: ABAssignment | None) -> Iterator[ABAssignment | None]:
    """在作用域内绑定分流结论，退出时复位。"""
    token: Token[ABAssignment | None] = _assignment_var.set(assignment)
    try:
        yield assignment
    finally:
        _assignment_var.reset(token)


def peek_ab_metadata() -> dict[str, Any]:
    """当前分流的可观测字段（供 span / 日志归因）。

    未命中实验时返回空 dict —— 调用方可直接 update 进 metadata，
    不会给基线流量塞入空字段。
    """
    assignment = get_ab_assignment()
    if assignment is None or not assignment.hit:
        return {}
    fields: dict[str, Any] = {
        "ab_experiment": assignment.experiment,
        "ab_arm": assignment.arm,
        "ab_bucket": assignment.bucket,
        "ab_prompt_variant": assignment.prompt_variant,
        "ab_max_iterations": assignment.max_iterations,
    }
    if assignment.correlation_id:
        fields["ab_correlation_id"] = assignment.correlation_id
    return fields


def effective_max_iterations(default: int) -> int:
    """实验对 Agent Loop 迭代上限的覆写（无覆写 / 越界时回落默认）。

    硬上限 ``_MAX_ITERATIONS_CEILING``：迭代上限直接决定单次请求的
    LLM 调用次数与费用，实验配置是运维手写的 JSON，必须夹紧。
    """
    assignment = get_ab_assignment()
    if assignment is None or assignment.max_iterations is None:
        return default
    value = assignment.max_iterations
    if not 1 <= value <= _MAX_ITERATIONS_CEILING:
        return default
    return value


def active_prompt_variant() -> str:
    """当前请求生效的 prompt 变体名（未命中实验时返回空串）。"""
    assignment = get_ab_assignment()
    if assignment is None or not assignment.hit:
        return ""
    return assignment.prompt_variant


def bind_ab_assignment(assignment: ABAssignment | None) -> None:
    """把分流结论绑定到当前请求上下文（请求级，无需手动复位）。

    为什么可以不复位：ASGI 每个请求在自己的 asyncio Task 中处理，
    Task 创建时复制上下文，因此请求内 ``set`` 的值不会外泄到其它请求。
    绑定之后，调用链深处的 Generator / Agent Loop 都能读到同一结论，
    不必为实验参数往 ``engine.answer()`` 已有的十几个参数里再加一个。

    需要严格作用域（评测、单测里反复切换分组）时请用 :func:`ab_scope`。
    """
    _assignment_var.set(assignment)
