"""
Agent 轨迹审计视图 — 从扁平 Span 证据还原可读的执行序列。

背景：
    SpanRecorder 收集的 span 按执行顺序追加（append 顺序即时间序），因此
    遍历该列表天然就是一次完整 Agent 轨迹。本模块据此重建一条带轮次、
    含重复调用的可读轨迹，供调试 / 失败 case 审计输出使用（评测.md §4.4
    的只读视图，非评分依据）。

    - 保留重复调用（与 tool_selection_metrics.extract_called_tools 的
      "首现去重链"不同，这里不丢信息，便于观察兜圈/重复调用）；
    - 从 span name 的 _iter{N} 后缀解析轮次（节点 span 形如
      retrieve_iter1）；子 span（permission.decision / compaction_event 等）
      无该后缀，继承其所在节点 span 的轮次；
    - tool.call span 额外提取工具名（name 形如 tool:{name}）。

设计约束：
    - 纯函数、零外部依赖、零 LLM 成本；
    - 不新增任何评分指标，仅用于审计 / 调试可读性。
"""

from __future__ import annotations

import re
from typing import Any

#: 节点 span 轮次后缀（如 retrieve_iter1 → 1）
_ITER_RE = re.compile(r"_iter(\d+)$")
#: tool.call 审计 span 的 name 前缀（engine._execute_tool_use 写入）
_TOOL_PREFIX = "tool:"

#: 标准 SpanType → 可读短标签（便于阅读轨迹链）
_LABELS: dict[str, str] = {
    "task.run": "task",
    "plan.create": "think",
    "context.load": "retrieve",
    "tool.call": "tool",
    "state.update": "generate",
    "score.compute": "reflect",
}


def build_trajectory(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从扁平 Span 证据重建可读执行轨迹（保序、含重复调用与轮次）。

    Args:
        spans: SpanRecord.to_dict() 字典列表（EvalCaseResult.spans）。

    Returns:
        按执行顺序排列的步骤列表；根 span（task.run）被跳过。每步字段：
        order（全局顺序）/ iteration（轮次，子 span 继承）/ label /
        span_type / name / status / error / latency_ms / tool / ref_count。
    """
    steps: list[dict[str, Any]] = []
    cur_iter: int = 0
    for i, s in enumerate(spans):
        name = s.get("name") or ""
        span_type = s.get("span_type") or ""
        # 根 span 只是容器，不进入审计链
        if span_type == "task.run":
            continue

        # 节点 span 自带轮次；子 span 继承当前轮次
        m = _ITER_RE.search(name)
        if m:
            cur_iter = int(m.group(1))

        md = s.get("metadata") or {}
        refs = md.get("included_refs") if isinstance(md, dict) else None
        steps.append(
            {
                "order": i,
                "iteration": cur_iter,
                "label": _LABELS.get(span_type, span_type),
                "span_type": span_type,
                "name": name,
                "status": s.get("status") or "ok",
                "error": s.get("error"),
                "latency_ms": s.get("latency_ms"),
                "tool": _tool_of(name, span_type),
                "ref_count": len(refs) if isinstance(refs, (list, tuple)) else 0,
            }
        )
    return steps


def format_trajectory(steps: list[dict[str, Any]]) -> str:
    """将轨迹步骤压缩为一行人类可读链（调试 / 审计日志用）。

    例::

        [i0] think -> retrieve(3) -> tool:kb_search -> generate -> reflect | [i1] think -> generate
    """
    parts: list[str] = []
    cur_iter: int | None = None
    for st in steps:
        it = st["iteration"]
        if cur_iter is None or it != cur_iter:
            parts.append(f"[i{it}]")
            cur_iter = it

        label = st["label"]
        if st["tool"]:
            label = f"tool:{st['tool']}"
        elif label == "retrieve" and st["ref_count"]:
            label = f"retrieve({st['ref_count']})"
        if st["status"] == "error":
            label = f"{label}!(err)"
        parts.append(label)
    return " -> ".join(parts)


def _tool_of(name: str, span_type: str) -> str | None:
    """从 tool.call span 提取工具名；非工具 span 返回 None。"""
    if span_type != "tool.call":
        return None
    if name.startswith(_TOOL_PREFIX):
        tool = name[len(_TOOL_PREFIX):].strip()
        return tool or None
    return None
