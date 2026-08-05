"""工具选择准确度评测维度（P1-4）— 标注式评估 Agent 是否选对工具。

评测体系此前只捕获最终答案，不评估「给定 query，Agent 是否调用了正确的
工具」。TrailAggregator 的 tool_call_rate / tool_hit_rate 是在线监控指标
（会话级聚合），不是离线 case 级评测维度。本模块补齐该缺口：

数据来源（零额外 LLM 成本，纯 Span 证据）：
    Agent 执行期间，``_execute_tool_use`` 为每次工具调用记录标准
    ``tool.call`` 审计 Span，其 ``name`` 形如 ``tool:{tool_name}``
    （engine.py:_execute_tool_use）。据此提取本 case 实际调用的工具集合，
    与数据集标注 ``expected_tools`` / ``forbidden_tools`` 对比。

指标定义（与检索层 precision/recall 同口径，便于横向理解）：
    - recall = |expected ∩ called| / |expected|
        期望调用的工具中，实际调用了多少（漏调即 recall 不足）
    - precision = |expected ∩ called| / |called|
        实际调用的工具中，有多少是期望的（多调 / 误调拉低 precision；
        forbidden_tools 天然落入「非期望」从而拉低 precision）
    - f1 = 2*p*r/(p+r)
    - expected_missing = expected - called（应调未调的工具）
    - forbidden_called = forbidden ∩ called（不应调却调了的工具）
    - passed = 无 expected_missing 且无 forbidden_called

适用条件：expected_tools 或 forbidden_tools 非空时才计算；二者均空返回 None。
"""

from __future__ import annotations

from typing import Any

#: tool.call 审计 Span 的 name 前缀（engine._execute_tool_use 写入）
_TOOL_SPAN_NAME_PREFIX = "tool:"


def extract_called_tools(spans: list[dict[str, Any]]) -> list[str]:
    """从标准 Span 证据提取本 case 实际调用的工具名列表（去重保序）。

    识别规则：``span_type == "tool.call"`` 且 ``name`` 形如 ``tool:{name}``。
    排除 LangGraph 节点 Span（name 形如 ``tool_call_iter{N}``，代表迭代节点
    而非具体工具调用）。

    Args:
        spans: SpanRecord.to_dict() 字典列表（EvalCaseResult.spans）。

    Returns:
        去重后保持首次出现顺序的工具名列表；无工具调用时为空列表。
    """
    called: list[str] = []
    seen: set[str] = set()
    for s in spans:
        span_type = s.get("span_type") or ""
        if span_type != "tool.call":
            continue
        name = s.get("name") or ""
        if not name.startswith(_TOOL_SPAN_NAME_PREFIX):
            continue
        tool_name = name[len(_TOOL_SPAN_NAME_PREFIX):].strip()
        if not tool_name or tool_name in seen:
            continue
        seen.add(tool_name)
        called.append(tool_name)
    return called


def compute_tool_selection_metrics(
    called_tools: list[str],
    expected_tools: list[str] | None,
    forbidden_tools: list[str] | None,
) -> dict[str, Any] | None:
    """计算工具选择准确度指标（P1-4）。

    Args:
        called_tools: 实际调用的工具名列表（由 extract_called_tools 提取）。
        expected_tools: 期望调用的工具名列表（数据集标注）。
        forbidden_tools: 禁止调用的工具名列表（数据集标注，负样本）。

    Returns:
        含 precision/recall/f1/expected_missing/forbidden_called/passed 的字典；
        expected_tools 与 forbidden_tools 均为空时返回 None（不适用）。
    """
    expected = [str(t) for t in (expected_tools or []) if t]
    forbidden = [str(t) for t in (forbidden_tools or []) if t]
    if not expected and not forbidden:
        return None

    called_set = set(called_tools)
    expected_set = set(expected)
    forbidden_set = set(forbidden)

    expected_hit = expected_set & called_set
    recall = (
        len(expected_hit) / len(expected_set) if expected_set else 1.0
    )
    # precision：实际调用中有多少是期望的；无调用时——无期望则满分（什么都没
    # 调是对的），有期望但未调用则 0（全漏）
    if called_set:
        precision = len(expected_hit) / len(called_set)
    else:
        precision = 1.0 if not expected_set else 0.0

    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    expected_missing = [t for t in expected if t not in called_set]
    forbidden_called = [t for t in forbidden if t in called_set]
    passed = not expected_missing and not forbidden_called

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "called_tools": list(called_tools),
        "expected_missing": expected_missing,
        "forbidden_called": forbidden_called,
        "passed": passed,
    }


def aggregate_tool_selection_metrics(
    case_metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    """聚合 run 级工具选择准确度指标（P1-4）。

    Args:
        case_metrics: 各 case 的 tool_selection_metrics 字典列表（跳过 None）。

    Returns:
        run 级聚合指标；无标注 case 时返回空 dict。
    """
    if not case_metrics:
        return {}

    n = len(case_metrics)
    avg_precision = sum(m["precision"] for m in case_metrics) / n
    avg_recall = sum(m["recall"] for m in case_metrics) / n
    avg_f1 = sum(m["f1"] for m in case_metrics) / n
    passed_count = sum(1 for m in case_metrics if m["passed"])

    return {
        "tool_selection_case_count": n,
        "avg_tool_precision": round(avg_precision, 4),
        "avg_tool_recall": round(avg_recall, 4),
        "avg_tool_f1": round(avg_f1, 4),
        "tool_selection_pass_rate": round(passed_count / n, 4),
    }
