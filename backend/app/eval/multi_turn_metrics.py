"""多轮对话评测维度（P1-3）— 量化 Agent Loop 多轮行为质量。

评测体系此前以单轮 query→answer 为基本单元，``max_iterations`` 虽已参数化
（支持 think→execute→reflect 多轮循环），但只捕获最终答案，不评估中间迭代
的检索质量、反思质量与收敛速度。Agent 的核心价值在于多轮推理，本模块从
标准 Span 证据提取多轮行为指标，补齐该维度。

指标定义（纯 Span 证据，零额外 LLM 成本）：
    - iterations_used：实际迭代次数（root span metadata.iterations）
    - early_convergence：是否在达到 max_iterations 前收敛（True=高效）
    - convergence_efficiency：1 - iterations_used/max_iterations，越高越早收敛
    - retrieval_call_count：检索事件数（含 included_refs 的 span）
    - tool_call_count：工具调用数（span_type=tool.call）
    - plan_create_count：规划/思考数（span_type=plan.create）
    - avg_retrieval_per_iteration：每轮平均检索引用数
    - retrieval_redundancy_ratio：跨轮重复检索比例（越高越冗余）
      = (总引用数 - 去重引用数) / 总引用数

适用条件：iterations_used > 1 时才计算（单轮检索路径无多轮行为）。
"""

from __future__ import annotations

from typing import Any


def extract_multi_turn_metrics(
    spans: list[dict[str, Any]],
    max_iterations: int,
    iterations_used: int | None,
) -> dict[str, Any] | None:
    """从标准 Span 证据提取多轮对话行为指标（P1-3）。

    Args:
        spans: SpanRecord.to_dict() 字典列表（EvalCaseResult.spans）。
        max_iterations: 本次评测的 Agent Loop 迭代上限。
        iterations_used: 实际迭代次数（root span metadata.iterations）。

    Returns:
        多轮行为指标字典；单轮路径（iterations_used <= 1 或 None）返回 None。
    """
    if iterations_used is None or iterations_used <= 1:
        return None

    retrieval_refs: list[str] = []
    retrieval_call_count = 0
    tool_call_count = 0
    plan_create_count = 0

    for s in spans:
        span_type = s.get("span_type") or ""
        metadata = s.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        # 检索事件：context.load span 或任何携带 included_refs 的 span
        included = metadata.get("included_refs")
        if span_type == "context.load" or included:
            retrieval_call_count += 1
            for ref in included or []:
                retrieval_refs.append(str(ref))

        if span_type == "tool.call":
            tool_call_count += 1
        elif span_type == "plan.create":
            plan_create_count += 1

    total_refs = len(retrieval_refs)
    unique_refs = len(set(retrieval_refs))
    redundancy = (
        (total_refs - unique_refs) / total_refs if total_refs > 0 else 0.0
    )

    early_convergence = iterations_used < max_iterations
    convergence_efficiency = (
        1.0 - (iterations_used / max_iterations) if max_iterations > 0 else 0.0
    )

    return {
        "iterations_used": iterations_used,
        "max_iterations": max_iterations,
        "early_convergence": early_convergence,
        "convergence_efficiency": round(max(0.0, convergence_efficiency), 4),
        "retrieval_call_count": retrieval_call_count,
        "tool_call_count": tool_call_count,
        "plan_create_count": plan_create_count,
        "avg_retrieval_per_iteration": (
            round(total_refs / iterations_used, 4) if iterations_used else 0.0
        ),
        "retrieval_redundancy_ratio": round(redundancy, 4),
    }


def aggregate_multi_turn_metrics(
    case_metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    """聚合 run 级多轮对话指标（P1-3）。

    Args:
        case_metrics: 各 case 的 multi_turn_metrics 字典列表（跳过 None）。

    Returns:
        run 级聚合指标；无多轮 case 时返回空 dict。
    """
    if not case_metrics:
        return {}

    n = len(case_metrics)
    avg_iterations = sum(m["iterations_used"] for m in case_metrics) / n
    early_rate = sum(1 for m in case_metrics if m["early_convergence"]) / n
    avg_redundancy = sum(m["retrieval_redundancy_ratio"] for m in case_metrics) / n
    avg_efficiency = sum(m["convergence_efficiency"] for m in case_metrics) / n
    total_retrieval = sum(m["retrieval_call_count"] for m in case_metrics)
    total_tool = sum(m["tool_call_count"] for m in case_metrics)

    return {
        "multi_turn_case_count": n,
        "avg_iterations": round(avg_iterations, 4),
        "early_convergence_rate": round(early_rate, 4),
        "avg_convergence_efficiency": round(avg_efficiency, 4),
        "avg_retrieval_redundancy": round(avg_redundancy, 4),
        "total_retrieval_calls": total_retrieval,
        "total_tool_calls": total_tool,
    }
