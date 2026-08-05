"""
Agent 评测标准 Span 类型枚举 — 统一 Trace/Span 事件模型的类型定义。

trace = one task run（一次完整任务）
span  = one meaningful operation（一次有意义的操作）

设计要点：
    - 字符串枚举，值直接作为 span.type 持久化与查询；
    - 覆盖 Agent Loop 全生命周期：上下文加载 → 规划 → 工具 → 观测 →
      状态 → 记忆 → 权限 → 检查点 → 失败恢复 → 评分 → 人工复核；
    - 与 OpenTelemetry 模型对齐，便于后续导出到 OTel/LangFuse。
"""

from __future__ import annotations

from enum import Enum


class SpanType(str, Enum):
    """Agent 评测的标准 Span 类型。"""

    CONTEXT_LOAD = "context.load"  # 上下文加载
    PLAN_CREATE = "plan.create"  # 规划生成（think 节点）
    TOOL_CALL = "tool.call"  # 工具调用
    TOOL_OBSERVE = "tool.observe"  # 工具结果观测
    STATE_UPDATE = "state.update"  # 状态更新
    MEMORY_READ = "memory.read"  # 记忆读取
    MEMORY_WRITE = "memory.write"  # 记忆写入
    PERMISSION_DECISION = "permission.decision"  # 权限决策
    CHECKPOINT_CREATE = "checkpoint.create"  # 检查点创建
    FAILURE_RECOVER = "failure.recover"  # 失败恢复
    SCORE_COMPUTE = "score.compute"  # 评分计算
    REVIEW_HUMAN = "review.human"  # 人工复核
