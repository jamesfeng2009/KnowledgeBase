"""
离线评测 Runner — 单一职责：执行评测并计算检索层 + 生成层指标。

检索层指标（纯数学，不调 LLM）：
    - recall_at_k：Recall@K，前 K 条结果中命中相关文档的比例；
    - mrr：Mean Reciprocal Rank，第一个相关文档位置的倒数；
    - ndcg：NDCG@K，归一化折损累积增益（二值相关性）。

生成层指标（可选，调 LLM）：
    - 复用 LLMJudgeService 对生成答案打分，汇总 avg_judge_score。

设计要点：
    - engine 为 None 时只测检索指标（无检索能力时指标降级为 0，不抛异常）；
    - judge_service 为 None 或不可用时跳过生成层评分；
    - 单条用例异常不中断整体评测，记录 error 后继续；
    - 遵循优雅降级：所有外部调用均 try/except 包裹。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

#: 检索指标默认 K 值
_DEFAULT_K: int = 5


# ======================================================================
# Span 成本聚合（P1-5：延迟与 token 成本指标）
# ======================================================================


def extract_cost_from_spans(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """从标准 Span 记录聚合 case 级延迟与 token 成本（P1-5）。

    聚合策略：
        - latency_ms：wall-clock = max(end_time) - min(start_time)（全 span 覆盖，
          避免嵌套 span 重复累加）；
        - total_tokens：优先取根 span metadata.total_tokens（engine 已汇总真实
          LLM 用量），缺省时回退求和各 span cost.token_count；
        - iterations：取根 span metadata.iterations（多轮迭代次数，P1-3 复用）。

    Args:
        spans: SpanRecord.to_dict() 字典列表（EvalCaseResult.spans）。

    Returns:
        ``{"latency_ms": float|None, "total_tokens": int, "iterations": int|None}``。
        无 span 证据时 latency_ms 为 None、total_tokens 为 0。
    """
    if not spans:
        return {"latency_ms": None, "total_tokens": 0, "iterations": None}

    start_times: list[float] = []
    end_times: list[float] = []
    for s in spans:
        st = s.get("start_time")
        et = s.get("end_time")
        if isinstance(st, (int, float)):
            start_times.append(float(st))
        if isinstance(et, (int, float)):
            end_times.append(float(et))

    latency_ms: float | None = None
    if start_times and end_times:
        # span 时间戳为秒，latency 转毫秒（与 SpanRecord.latency_ms 口径一致）
        latency_ms = round((max(end_times) - min(start_times)) * 1000, 2)
        if latency_ms < 0:
            latency_ms = None

    # total_tokens：根 span（parent_span_id 为 None）metadata.total_tokens 优先
    total_tokens = 0
    iterations: int | None = None
    root_meta: dict[str, Any] | None = None
    for s in spans:
        if s.get("parent_span_id") is None:
            root_meta = s.get("metadata") or {}
            if isinstance(root_meta, dict):
                tt = root_meta.get("total_tokens")
                if isinstance(tt, (int, float)) and tt > 0:
                    total_tokens = int(tt)
                it = root_meta.get("iterations")
                if isinstance(it, int):
                    iterations = it
            break

    # 回退：根 span 未汇总时，求和各 span cost.token_count（近似，可能含子 span）
    if total_tokens == 0:
        for s in spans:
            cost = s.get("cost") or {}
            tc = cost.get("token_count") if isinstance(cost, dict) else None
            if isinstance(tc, (int, float)) and tc > 0:
                total_tokens += int(tc)

    return {
        "latency_ms": latency_ms,
        "total_tokens": total_tokens,
        "iterations": iterations,
    }


def _percentile(sorted_values: list[float], pct: int) -> float:
    """计算已升序排列序列的百分位数（最近秩法，P1-5 P99 延迟用）。

    Args:
        sorted_values: 已升序排列的数值列表。
        pct: 百分位（0-100）。

    Returns:
        百分位数值；空列表返回 0.0。
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    # 最近秩法：rank = ceil(pct/100 * N)，取 rank-1 索引
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    idx = min(rank - 1, len(sorted_values) - 1)
    return float(sorted_values[idx])


# ======================================================================
# 检索层指标（纯数学函数）
# ======================================================================


def recall_at_k(
    retrieved_ids: list[str], relevant_ids: list[str], k: int
) -> float:
    """计算 Recall@K — 前 K 条检索结果中命中相关文档的比例。

    Args:
        retrieved_ids: 检索返回的文档 ID 列表（按相关性排序）。
        relevant_ids: 相关文档 ID 列表（ground truth）。
        k: 截断位置。

    Returns:
        Recall@K，取值 [0, 1]。relevant_ids 为空或 k<=0 时返回 0.0。
    """
    if not relevant_ids or k <= 0:
        return 0.0
    top_k = list(retrieved_ids)[:k]
    relevant_set = set(relevant_ids)
    hits = sum(1 for doc_id in top_k if doc_id in relevant_set)
    return hits / len(relevant_set)


def mrr(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """计算 MRR（Mean Reciprocal Rank）— 第一个相关文档位置的倒数。

    Args:
        retrieved_ids: 检索返回的文档 ID 列表。
        relevant_ids: 相关文档 ID 列表。

    Returns:
        1/rank（rank 从 1 开始），无匹配时返回 0.0。
    """
    if not relevant_ids or not retrieved_ids:
        return 0.0
    relevant_set = set(relevant_ids)
    for i, doc_id in enumerate(retrieved_ids):
        if doc_id in relevant_set:
            return 1.0 / (i + 1)
    return 0.0


def ndcg(
    retrieved_ids: list[str], relevant_ids: list[str], k: int
) -> float:
    """计算 NDCG@K（二值相关性）— 归一化折损累积增益。

    DCG@K  = sum_{i=1}^{K} rel_i / log2(i + 1)
    IDCG@K = sum_{i=1}^{min(K, |R|)} 1 / log2(i + 1)
    NDCG@K = DCG / IDCG

    Args:
        retrieved_ids: 检索返回的文档 ID 列表。
        relevant_ids: 相关文档 ID 列表。
        k: 截断位置。

    Returns:
        NDCG@K，取值 [0, 1]。relevant_ids 为空或 k<=0 时返回 0.0。
    """
    if not relevant_ids or k <= 0:
        return 0.0
    relevant_set = set(relevant_ids)

    # DCG：遍历前 K 条检索结果，命中记 1
    dcg = 0.0
    for i, doc_id in enumerate(retrieved_ids[:k]):
        if doc_id in relevant_set:
            dcg += 1.0 / math.log2(i + 2)  # i 从 0 开始，rank=i+1，log2(rank+1)=log2(i+2)

    # IDCG：理想情况下相关文档全部排在最前
    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))

    if idcg <= 0:
        return 0.0
    return dcg / idcg


# ======================================================================
# 评测结果数据类
# ======================================================================


@dataclass
class EvalCaseResult:
    """单条用例的评测结果。

    Attributes:
        query: 用户问题。
        case_id: 用例唯一标识（P2-2：基线 case 级匹配优先使用，
            避免重复 query 互相覆盖；缺省回退按 query 匹配）。
        retrieved_doc_ids: 实际检索返回的文档 ID 列表。
        recall_at_5: Recall@5。
        mrr: MRR。
        ndcg_at_5: NDCG@5。
        answer: 生成的答案（未启用生成时为 None）。
        judge_scores: LLM Judge 评分字典（未启用或失败时为 None）。
        ragas_scores: RAGAS 四项标准指标（未启用或失败时为 None；
            单项指标无法计算时该键值为 None）。
        error: 异常信息（正常时为 None）。
        spans: 本次用例执行收集到的标准 Span 记录（dict 列表，评测.md §4.4）。
        rule_scores: 规则评分结果（§5.6：negative 拒答判定 / golden 检查点评分，
            无规则评分需求的用例为 None）。
        context_metrics: 上下文质量四类分数（§7.3 recall/precision/freshness/
            robustness + 失败明细，用例无 context_expect 时为 None）。
        latency_ms: case 级 wall-clock 延迟（毫秒，P1-5；无 span 证据时为 None）。
        token_usage: token 成本汇总（P1-5；含 total_tokens，无证据时为 None）。
        iterations: Agent Loop 实际迭代次数（P1-3/P1-5；从根 span metadata 提取，
            单轮检索路径为 None）。
        compression_metrics: 压缩信息损耗评估（P1-6；含关键实体保留率，
            本 case 未触发压缩时为 None）。
        multi_turn_metrics: 多轮对话行为指标（P1-3；含迭代次数/收敛效率/
            检索冗余，单轮检索路径为 None）。
        tool_selection_metrics: 工具选择准确度（P1-4 标注式；含 precision/
            recall/f1/expected_missing/forbidden_called，无工具标注时为 None）。
    """

    query: str
    case_id: str = ""
    retrieved_doc_ids: list[str] = field(default_factory=list)
    recall_at_5: float = 0.0
    mrr: float = 0.0
    ndcg_at_5: float = 0.0
    answer: str | None = None
    judge_scores: dict[str, Any] | None = None
    ragas_scores: dict[str, Any] | None = None
    error: str | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    rule_scores: dict[str, Any] | None = None
    context_metrics: dict[str, Any] | None = None
    latency_ms: float | None = None
    token_usage: dict[str, int] | None = None
    iterations: int | None = None
    compression_metrics: dict[str, Any] | None = None
    multi_turn_metrics: dict[str, Any] | None = None
    tool_selection_metrics: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        """单条用例是否通过 — 无错误且规则/上下文评分（如有）通过。"""
        if self.error is not None:
            return False
        if self.rule_scores is not None and self.rule_scores.get("passed") is False:
            return False
        if (
            self.context_metrics is not None
            and self.context_metrics.get("passed") is False
        ):
            return False
        if (
            self.tool_selection_metrics is not None
            and self.tool_selection_metrics.get("passed") is False
        ):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "case_id": self.case_id,
            "retrieved_doc_ids": self.retrieved_doc_ids,
            "recall_at_5": round(self.recall_at_5, 4),
            "mrr": round(self.mrr, 4),
            "ndcg_at_5": round(self.ndcg_at_5, 4),
            "answer": self.answer,
            "judge_scores": self.judge_scores,
            "ragas_scores": self.ragas_scores,
            "error": self.error,
            "passed": self.passed,
            "spans": self.spans,
            "rule_scores": self.rule_scores,
            "context_metrics": self.context_metrics,
            "latency_ms": self.latency_ms,
            "token_usage": self.token_usage,
            "iterations": self.iterations,
            "compression_metrics": self.compression_metrics,
            "multi_turn_metrics": self.multi_turn_metrics,
            "tool_selection_metrics": self.tool_selection_metrics,
        }


@dataclass
class EvalRunResult:
    """一次评测运行的汇总结果。

    Attributes:
        case_results: 各用例结果列表。
        avg_recall_at_5: 平均 Recall@5（P2-2：错误用例按 0 计入，不静默剔除）。
        avg_mrr: 平均 MRR。
        avg_ndcg_at_5: 平均 NDCG@5。
        avg_judge_score: 平均 Judge 总分（未启用时为 0.0）。
        avg_ragas: RAGAS 四项指标均值（未启用时为空 dict；
            单项指标全部用例不可计算时该键缺省）。
        total: 用例总数。
        passed: 通过用例数。
        evaluated_at: 评测时间（ISO 字符串）。
        run_id: 运行 ID（UUID，由 runner 生成，便于持久化引用）。
        max_iterations: 本次评测使用的 Agent Loop 迭代上限（默认 5）。
        dataset_version: 数据集版本指纹（P2-2：sha1 前缀，基线对比时
            校验两侧数据集是否一致，防止不同数据集的结果误比）。
    """

    case_results: list[EvalCaseResult] = field(default_factory=list)
    avg_recall_at_5: float = 0.0
    avg_mrr: float = 0.0
    avg_ndcg_at_5: float = 0.0
    avg_judge_score: float = 0.0
    avg_ragas: dict[str, float] = field(default_factory=dict)
    total: int = 0
    passed: int = 0
    evaluated_at: str = ""
    run_id: str = ""
    max_iterations: int = 5
    dataset_version: str = ""
    # P1-5: 延迟与 token 成本聚合（生产级 RAG 的关键质量维度）
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    total_tokens: int = 0
    avg_total_tokens: float = 0.0
    # P1-3: 多轮对话行为指标聚合
    multi_turn_summary: dict[str, Any] = field(default_factory=dict)
    # P1-4: 工具选择准确度指标聚合
    tool_selection_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "max_iterations": self.max_iterations,
            "dataset_version": self.dataset_version,
            "case_results": [c.to_dict() for c in self.case_results],
            "avg_recall_at_5": round(self.avg_recall_at_5, 4),
            "avg_mrr": round(self.avg_mrr, 4),
            "avg_ndcg_at_5": round(self.avg_ndcg_at_5, 4),
            "avg_judge_score": round(self.avg_judge_score, 4),
            "avg_ragas": {k: round(v, 4) for k, v in self.avg_ragas.items()},
            "total": self.total,
            "passed": self.passed,
            "evaluated_at": self.evaluated_at,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "p99_latency_ms": round(self.p99_latency_ms, 2),
            "total_tokens": self.total_tokens,
            "avg_total_tokens": round(self.avg_total_tokens, 2),
            "multi_turn_summary": self.multi_turn_summary,
            "tool_selection_summary": self.tool_selection_summary,
        }


# ======================================================================
# 评测 Runner
# ======================================================================


class EvalRunner:
    """离线评测执行器 — 编排检索 + 生成 + 评分。

    使用方式::

        runner = EvalRunner(engine=rag_engine, judge_service=judge)
        result = await runner.run(dataset, with_generation=True)

    engine / judge_service / ragas_metrics 均可选，缺省时优雅降级：
        - engine 为 None：检索无能力，指标降级为 0；
        - judge_service 为 None：跳过生成层 Judge 评分；
        - ragas_metrics 为 None：跳过 RAGAS 标准指标。

    max_iterations 控制传入 Agent 状态的迭代上限（默认 5，与引擎默认值一致）。
    历史上此处硬编码为 1，导致评测只能覆盖单轮检索+生成，无法评估
    think → execute → reflect 多轮循环；现已参数化解除该限制。
    """

    def __init__(
        self,
        engine: Any | None = None,
        judge_service: Any | None = None,
        ragas_metrics: Any | None = None,
        max_iterations: int = 5,
    ) -> None:
        # 延迟类型标注避免循环导入：engine 为 AgenticRAGEngine，judge 为 LLMJudgeService
        self.engine = engine
        self.judge_service = judge_service
        self.ragas_metrics = ragas_metrics
        # Agent Loop 迭代上限 — 透传给评测 state，支持多轮任务级评估
        self.max_iterations = max(1, max_iterations)

    async def run(
        self,
        dataset: Any,
        kb_ids: list[str] | None = None,
        with_generation: bool = True,
        dataset_version: str = "",
    ) -> EvalRunResult:
        """对数据集执行评测。

        Args:
            dataset: EvalDataset 实例。
            kb_ids: 运行级知识库限定（用例自带 kb_ids 时优先用例级）。
            with_generation: 是否启用生成 + Judge 评分（False 时只测检索指标）。
            dataset_version: 数据集版本指纹（P2-2，随结果持久化，
                基线对比时校验两侧数据集一致性）。

        Returns:
            EvalRunResult 汇总结果。
        """
        run_id = str(uuid.uuid4())
        case_results: list[EvalCaseResult] = []

        # 数据集可能传入 list[EvalCase] 或 EvalDataset，统一取迭代器
        cases = list(dataset) if not hasattr(dataset, "cases") else dataset.cases

        # P2-2: 未显式传入时自动计算数据集指纹（EvalDataset 提供 fingerprint()）
        if not dataset_version and hasattr(dataset, "fingerprint"):
            try:
                dataset_version = dataset.fingerprint()
            except Exception:  # pragma: no cover - 防御性降级
                dataset_version = ""

        if self.engine is None:
            log.warning("eval_runner.engine_none", msg="engine 未注入，检索指标将降级为 0")

        for case in cases:
            case_result = await self._eval_case(case, kb_ids, with_generation)
            case_results.append(case_result)

        # 汇总
        # P2-2 口径修复：检索层指标对全部用例求均值 —— 错误用例按 0 计入，
        # 不再静默剔除（此前只统计 error is None 的用例，系统性抬高均值）。
        total = len(case_results)
        n = total if total else 1

        avg_recall = sum(c.recall_at_5 for c in case_results) / n
        avg_mrr_val = sum(c.mrr for c in case_results) / n
        avg_ndcg = sum(c.ndcg_at_5 for c in case_results) / n

        # Judge 评分均值：仅统计有 judge_scores 的用例
        # （生成失败的用例无分可评，不计入而非按 0 拉低 —— 该缺失由
        # passed 计数与 error 字段显式暴露）
        judged = [c for c in case_results if c.judge_scores is not None]
        avg_judge = 0.0
        if judged:
            scores = [
                float(c.judge_scores.get("total_score", 0.0))  # type: ignore[union-attr]
                for c in judged
            ]
            avg_judge = sum(scores) / len(scores)

        # RAGAS 指标均值：仅统计有 ragas_scores 的用例；
        # P2-1: 单项指标为 None（无 expected_answer 不可计算）时不参与均值
        ragas_list = [c for c in case_results if c.ragas_scores is not None]
        avg_ragas: dict[str, float] = {}
        if ragas_list:
            ragas_keys = {"faithfulness", "answer_relevancy", "context_precision", "context_recall"}
            for key in ragas_keys:
                values = [
                    float(c.ragas_scores[key])  # type: ignore[index]
                    for c in ragas_list
                    if c.ragas_scores is not None
                    and c.ragas_scores.get(key) is not None
                ]
                if values:
                    avg_ragas[key] = sum(values) / len(values)

        passed = sum(1 for c in case_results if c.passed)

        # P1-5: 延迟与 token 成本聚合（生产级 RAG 的关键质量维度）
        latencies = [c.latency_ms for c in case_results if c.latency_ms is not None]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        p99_latency = _percentile(sorted(latencies), 99) if latencies else 0.0
        token_counts = [
            (c.token_usage or {}).get("total_tokens", 0) for c in case_results
        ]
        total_tokens = sum(token_counts)
        avg_tokens = total_tokens / n if total_tokens else 0.0

        # P1-3: 多轮对话行为指标聚合
        from app.eval.multi_turn_metrics import aggregate_multi_turn_metrics

        multi_turn_summary = aggregate_multi_turn_metrics(
            [c.multi_turn_metrics for c in case_results if c.multi_turn_metrics]
        )

        # P1-4: 工具选择准确度指标聚合
        from app.eval.tool_selection_metrics import aggregate_tool_selection_metrics

        tool_selection_summary = aggregate_tool_selection_metrics(
            [c.tool_selection_metrics for c in case_results if c.tool_selection_metrics]
        )

        result = EvalRunResult(
            case_results=case_results,
            avg_recall_at_5=avg_recall,
            avg_mrr=avg_mrr_val,
            avg_ndcg_at_5=avg_ndcg,
            avg_judge_score=avg_judge,
            avg_ragas=avg_ragas,
            total=total,
            passed=passed,
            evaluated_at=datetime.utcnow().isoformat(),
            run_id=run_id,
            max_iterations=self.max_iterations,
            dataset_version=dataset_version,
            avg_latency_ms=avg_latency,
            p99_latency_ms=p99_latency,
            total_tokens=total_tokens,
            avg_total_tokens=avg_tokens,
            multi_turn_summary=multi_turn_summary,
            tool_selection_summary=tool_selection_summary,
        )

        log.info(
            "eval_runner.done",
            run_id=run_id,
            total=total,
            passed=passed,
            avg_recall_at_5=round(avg_recall, 4),
            avg_mrr=round(avg_mrr_val, 4),
            avg_ndcg_at_5=round(avg_ndcg, 4),
            avg_judge_score=round(avg_judge, 4),
            avg_ragas={k: round(v, 4) for k, v in avg_ragas.items()},
            avg_latency_ms=round(avg_latency, 2),
            p99_latency_ms=round(p99_latency, 2),
            total_tokens=total_tokens,
        )
        return result

    # ------------------------------------------------------------------
    # 单条用例评测
    # ------------------------------------------------------------------

    async def _eval_case(
        self,
        case: Any,
        run_kb_ids: list[str] | None,
        with_generation: bool,
    ) -> EvalCaseResult:
        """评测单条用例 — 检索 → 指标计算 → （可选）生成 + Judge。

        全程注入 SpanRecorder（contextvar），engine 内 @trace_node 埋点
        自动双写到本地标准 SpanRecord，实现轨迹级评测数据收集（§4.4）。
        """
        from app.observability.span_record import span_recorder

        with span_recorder() as recorder:
            result = await self._eval_case_inner(
                case, run_kb_ids, with_generation, recorder=recorder
            )
            try:
                collected = recorder.collect()
                result.spans = [s.to_dict() for s in collected]
                # P1-5: 从 span 证据聚合 case 级延迟 / token 成本 / 迭代次数
                cost_summary = extract_cost_from_spans(result.spans)
                result.latency_ms = cost_summary["latency_ms"]
                result.iterations = cost_summary["iterations"]
                total_tokens = cost_summary["total_tokens"]
                if total_tokens > 0:
                    result.token_usage = {"total_tokens": total_tokens}

                # P1-3: 多轮对话行为指标（仅多轮 case 计算）
                from app.eval.multi_turn_metrics import extract_multi_turn_metrics

                result.multi_turn_metrics = extract_multi_turn_metrics(
                    result.spans, self.max_iterations, result.iterations
                )

                # P1-4: 工具选择准确度（标注式：expected_tools/forbidden_tools 非空才计算）
                expected_tools = list(getattr(case, "expected_tools", []) or [])
                forbidden_tools = list(getattr(case, "forbidden_tools", []) or [])
                if expected_tools or forbidden_tools:
                    from app.eval.tool_selection_metrics import (
                        compute_tool_selection_metrics,
                        extract_called_tools,
                    )

                    called_tools = extract_called_tools(result.spans)
                    result.tool_selection_metrics = compute_tool_selection_metrics(
                        called_tools, expected_tools, forbidden_tools
                    )

                # 上下文质量评分（§7.3）：由 Span 证据聚合 ContextTraceRecord 后计算
                context_expect = getattr(case, "context_expect", None)
                if context_expect:
                    from app.eval.context_metrics import compute_context_metrics
                    from app.eval.context_trace import ContextTraceRecord

                    trace = ContextTraceRecord.from_spans(collected)
                    result.context_metrics = compute_context_metrics(
                        trace, context_expect
                    )

                # P1-6: 压缩信息损耗评估 — 本 case 触发过压缩时，从
                # ContextBudgetManager 快照计算关键实体保留率（零 LLM 成本）
                self._eval_compression_metrics(result, collected)
            except Exception as exc:  # pragma: no cover - 防御性降级
                log.warning("eval_runner.span_collect_error", error=str(exc))
            return result

    async def _eval_case_inner(
        self,
        case: Any,
        run_kb_ids: list[str] | None,
        with_generation: bool,
        recorder: Any | None = None,
    ) -> EvalCaseResult:
        """评测单条用例的实际执行体（由 _eval_case 包裹 span 收集）。

        P1-1 任务级化：with_generation=True 且 engine 具备 answer() 时，
        单次调用 answer() 完整执行 Agent Loop（think→retrieve→generate），
        检索证据从 Span metadata（included_refs/included_contents）提取 ——
        消除历史上「runner 双调 _retrieve + answer」导致的检索执行两遍、
        成本翻倍、两处 state 不一致问题。无检索证据时（缓存/FAQ 短路或
        非插桩引擎）降级为直调 _retrieve 补齐检索指标。
        """
        query: str = getattr(case, "query", "")
        case_id: str = str(getattr(case, "case_id", "") or "")
        expected_doc_ids: list[str] = list(getattr(case, "expected_doc_ids", []))
        expected_answer = getattr(case, "expected_answer", None)
        # 用例级 kb_ids 优先于运行级
        case_kb_ids = getattr(case, "kb_ids", None)
        kb_ids_for_case = case_kb_ids if case_kb_ids is not None else run_kb_ids

        retrieved_doc_ids: list[str] = []
        contexts: list[str] = []
        error: str | None = None
        answer: str | None = None

        has_answer_fn = (
            self.engine is not None
            and getattr(self.engine, "answer", None) is not None
        )

        if with_generation and has_answer_fn:
            # ---- P1-1 任务级路径：单次 answer() 完整执行 Agent Loop ----
            answer, error = await self._run_answer_once(query, kb_ids_for_case)

            # 从 Span 证据提取检索结果（最终一次 retrieve 的重排后引用）
            included_refs, included_contents = self._extract_retrieval_evidence(
                recorder
            )
            if included_refs:
                retrieved_doc_ids = included_refs
                contexts = included_contents
            else:
                # 降级：无检索证据（缓存/FAQ 短路命中 / 引擎未插桩）时
                # 直调 _retrieve 补齐检索指标，保证指标可计算。
                log.warning(
                    "eval_runner.no_retrieval_evidence",
                    query=query[:50],
                    msg="answer() 未产生 retrieve span 证据，回退直调 _retrieve",
                )
                retrieved_doc_ids, retrieved_docs, retr_error = (
                    await self._retrieve_only(query, kb_ids_for_case)
                )
                contexts = [
                    str(d.get("content", ""))
                    for d in retrieved_docs
                    if isinstance(d, dict) and d.get("content")
                ]
                if error is None:
                    error = retr_error
        elif self.engine is not None:
            # ---- 检索层路径（--no-generation）：直调 _retrieve，零 LLM 成本 ----
            retrieved_doc_ids, retrieved_docs, error = await self._retrieve_only(
                query, kb_ids_for_case
            )
            contexts = [
                str(d.get("content", ""))
                for d in retrieved_docs
                if isinstance(d, dict) and d.get("content")
            ]
            if with_generation and not has_answer_fn:
                # 需要生成但引擎无 answer 方法 —— 显式记录而非静默跳过
                gen_err = "engine 缺少 answer 方法"
                error = f"{error} | {gen_err}" if error else gen_err
        else:
            error = "engine_unavailable"

        # 2. 检索指标（纯数学）
        recall5 = recall_at_k(retrieved_doc_ids, expected_doc_ids, _DEFAULT_K)
        mrr_val = mrr(retrieved_doc_ids, expected_doc_ids)
        ndcg5 = ndcg(retrieved_doc_ids, expected_doc_ids, _DEFAULT_K)

        # 3. Judge + RAGAS（仅任务级路径产出 answer 后）
        judge_scores: dict[str, Any] | None = None
        ragas_scores: dict[str, Any] | None = None

        if answer is not None and self.judge_service is not None:
            judge_scores, judge_err = await self._judge_answer(
                query, answer, contexts
            )
            if judge_err is not None:
                error = f"{error} | {judge_err}" if error else judge_err

        if answer is not None and self.ragas_metrics is not None:
            try:
                ragas_scores = await self.ragas_metrics.evaluate(
                    query=query,
                    answer=answer,
                    contexts=contexts,
                    expected_answer=expected_answer,
                )
            except Exception as exc:
                log.warning("eval_runner.ragas_error", query=query[:50], error=str(exc))

        # 4. 规则评分（§5.6：negative 拒答判定 / golden 检查点评分，纯代码不调 LLM）
        rule_scores: dict[str, Any] | None = None
        try:
            from app.eval.refusal_metrics import evaluate_case_rules

            rule_scores = evaluate_case_rules(
                case_type=getattr(case, "case_type", "normal"),
                answer=answer,
                must_have_points=getattr(case, "must_have_points", None),
                forbidden_content=getattr(case, "forbidden_content", None),
            )
        except Exception as exc:
            log.warning("eval_runner.rule_score_error", query=query[:50], error=str(exc))

        return EvalCaseResult(
            query=query,
            case_id=case_id,
            retrieved_doc_ids=retrieved_doc_ids,
            recall_at_5=recall5,
            mrr=mrr_val,
            ndcg_at_5=ndcg5,
            answer=answer,
            judge_scores=judge_scores,
            ragas_scores=ragas_scores,
            error=error,
            rule_scores=rule_scores,
        )

    # ------------------------------------------------------------------
    # 任务级执行与证据提取（P1-1）
    # ------------------------------------------------------------------

    async def _run_answer_once(
        self,
        query: str,
        kb_ids_for_case: list[str] | None,
    ) -> tuple[str | None, str | None]:
        """单次调用 engine.answer() 完整执行 Agent Loop，返回 (answer, error)。

        只收集 str token（generate 阶段产物）；SSEEvent 进度事件不消费。
        检索/工具证据由 SpanRecorder 在后台同步收集，无需在此解析事件。
        """
        answer_parts: list[str] = []
        try:
            async for token in self.engine.answer(
                query,
                "eval-runner",
                f"eval-{uuid.uuid4()}",
                kb_ids=kb_ids_for_case,
                memory_context="",
            ):
                if isinstance(token, str):
                    answer_parts.append(token)
        except Exception as exc:
            log.warning("eval_runner.generate_error", query=query[:50], error=str(exc))
            return None, f"generate_error: {exc}"
        return ("".join(answer_parts) or None), None

    async def _retrieve_only(
        self,
        query: str,
        kb_ids_for_case: list[str] | None,
    ) -> tuple[list[str], list[dict[str, Any]], str | None]:
        """直调 engine._retrieve（零 LLM 成本）— --no-generation 检索层路径。

        Returns:
            (retrieved_doc_ids, retrieved_docs, error) 三元组。
        """
        retrieve_fn = getattr(self.engine, "_retrieve", None)
        if retrieve_fn is None:
            return [], [], "engine 缺少 _retrieve 方法"
        try:
            state: dict[str, Any] = {
                "query": query,
                "iteration": 1,
                "retrieved_docs": [],
                "tool_results": [],
                "session_id": f"eval-{uuid.uuid4()}",
                "user_id": "eval-runner",
                "max_iterations": self.max_iterations,
                "kb_ids": kb_ids_for_case,
                "messages": [],
                "answer": "",
            }
            await retrieve_fn(state, kb_ids_for_case)
            retrieved_docs = list(state.get("retrieved_docs", []) or [])
            retrieved_doc_ids = [
                str(d.get("doc_id"))
                for d in retrieved_docs
                if isinstance(d, dict) and d.get("doc_id")
            ]
            return retrieved_doc_ids, retrieved_docs, None
        except Exception as exc:
            log.warning("eval_runner.retrieve_error", query=query[:50], error=str(exc))
            return [], [], f"retrieve_error: {exc}"

    @staticmethod
    def _extract_retrieval_evidence(
        recorder: Any | None,
    ) -> tuple[list[str], list[str]]:
        """从 SpanRecorder 提取检索证据（included_refs / included_contents）。

        取最后一个 context.load Span（多轮检索时以最终重排结果为准）；
        无 retrieve 证据时回退 generate Span 的 included_refs（生成阶段
        实际使用的上下文引用，无内容摘要）。

        Returns:
            (included_refs, included_contents) 二元组，均无证据时为 ([], [])。
        """
        if recorder is None:
            return [], []
        spans = getattr(recorder, "spans", None) or []
        for span in reversed(spans):
            if getattr(span, "span_type", "") != "context.load":
                continue
            md = getattr(span, "metadata", {}) or {}
            refs = [str(r) for r in md.get("included_refs") or []]
            if refs:
                contents = [str(c) for c in md.get("included_contents") or []]
                return refs, contents
        # 回退：generate span（span_type=state.update，含 included_refs）
        for span in reversed(spans):
            if getattr(span, "span_type", "") != "state.update":
                continue
            md = getattr(span, "metadata", {}) or {}
            refs = [str(r) for r in md.get("included_refs") or []]
            if refs:
                return refs, []
        return [], []

    # ------------------------------------------------------------------
    # Judge 评分
    # ------------------------------------------------------------------

    async def _judge_answer(
        self,
        query: str,
        answer: str,
        contexts: list[str],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """用 judge_service 对答案评分，返回 (judge_scores, error)。"""
        try:
            eval_result = await self.judge_service.evaluate_single(
                query, answer, contexts
            )
            judge_scores = {
                "citation_accuracy": getattr(eval_result, "citation_accuracy", 0),
                "completeness": getattr(eval_result, "completeness", 0),
                "hallucination_inverse": getattr(
                    eval_result, "hallucination_inverse", 0
                ),
                "total_score": getattr(eval_result, "total_score", 0.0),
                "passed": getattr(eval_result, "passed", False),
            }
            return judge_scores, None
        except Exception as exc:
            log.warning("eval_runner.judge_error", query=query[:50], error=str(exc))
            return None, f"judge_error: {exc}"

    # ------------------------------------------------------------------
    # 压缩信息损耗评估（P1-6）
    # ------------------------------------------------------------------

    def _eval_compression_metrics(
        self,
        result: EvalCaseResult,
        collected: list[Any],
    ) -> None:
        """本 case 触发过上下文压缩时，计算关键实体保留率并写入 result。

        P1-6：将 compression_metrics 接入主评测流水线。检测本 case 的 span
        中是否存在 ``context.compact`` 事件；存在则说明 ContextBudgetManager
        在本 case 内触发过压缩，其 ``get_last_snapshot()`` 即为本 case 的
        压缩前后快照（compress() 写入快照后立即被 _record_compaction_evidence
        读取，二者同属本 case 执行）。

        零 LLM 成本：仅调用规则法 ``compute_entity_retention_from_snapshot``。
        ConsistencyResult 双跑评估需额外 LLM 调用，保持独立工具不接入主流程。

        Args:
            result: 当前 case 的评测结果（就地写入 compression_metrics）。
            collected: 本 case 收集到的 SpanRecord 对象列表。
        """
        # 仅当本 case 出现压缩 span 时才读取快照（避免跨 case 误用过期快照）
        has_compact = any(
            getattr(s, "span_type", "") == "context.compact" for s in collected
        )
        if not has_compact:
            return

        budget = getattr(self.engine, "_budget", None)
        if budget is None:
            return
        try:
            snapshot = budget.get_last_snapshot()
            from app.eval.compression_metrics import (
                compute_entity_retention_from_snapshot,
            )

            report = compute_entity_retention_from_snapshot(snapshot)
            if report is not None:
                result.compression_metrics = report.to_dict()
        except Exception as exc:  # pragma: no cover - 防御性降级
            log.warning(
                "eval_runner.compression_metrics_error",
                query=result.query[:50],
                error=str(exc),
            )
