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
        retrieved_doc_ids: 实际检索返回的文档 ID 列表。
        recall_at_5: Recall@5。
        mrr: MRR。
        ndcg_at_5: NDCG@5。
        answer: 生成的答案（未启用生成时为 None）。
        judge_scores: LLM Judge 评分字典（未启用或失败时为 None）。
        ragas_scores: RAGAS 四项标准指标（未启用或失败时为 None）。
        error: 异常信息（正常时为 None）。
        spans: 本次用例执行收集到的标准 Span 记录（dict 列表，评测.md §4.4）。
        rule_scores: 规则评分结果（§5.6：negative 拒答判定 / golden 检查点评分，
            无规则评分需求的用例为 None）。
        context_metrics: 上下文质量四类分数（§7.3 recall/precision/freshness/
            robustness + 失败明细，用例无 context_expect 时为 None）。
    """

    query: str
    retrieved_doc_ids: list[str] = field(default_factory=list)
    recall_at_5: float = 0.0
    mrr: float = 0.0
    ndcg_at_5: float = 0.0
    answer: str | None = None
    judge_scores: dict[str, Any] | None = None
    ragas_scores: dict[str, float] | None = None
    error: str | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    rule_scores: dict[str, Any] | None = None
    context_metrics: dict[str, Any] | None = None

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
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
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
        }


@dataclass
class EvalRunResult:
    """一次评测运行的汇总结果。

    Attributes:
        case_results: 各用例结果列表。
        avg_recall_at_5: 平均 Recall@5。
        avg_mrr: 平均 MRR。
        avg_ndcg_at_5: 平均 NDCG@5。
        avg_judge_score: 平均 Judge 总分（未启用时为 0.0）。
        avg_ragas: RAGAS 四项指标均值（未启用时为空 dict）。
        total: 用例总数。
        passed: 通过用例数。
        evaluated_at: 评测时间（ISO 字符串）。
        run_id: 运行 ID（UUID，由 runner 生成，便于持久化引用）。
        max_iterations: 本次评测使用的 Agent Loop 迭代上限（默认 5）。
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "max_iterations": self.max_iterations,
            "case_results": [c.to_dict() for c in self.case_results],
            "avg_recall_at_5": round(self.avg_recall_at_5, 4),
            "avg_mrr": round(self.avg_mrr, 4),
            "avg_ndcg_at_5": round(self.avg_ndcg_at_5, 4),
            "avg_judge_score": round(self.avg_judge_score, 4),
            "avg_ragas": {k: round(v, 4) for k, v in self.avg_ragas.items()},
            "total": self.total,
            "passed": self.passed,
            "evaluated_at": self.evaluated_at,
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
    ) -> EvalRunResult:
        """对数据集执行评测。

        Args:
            dataset: EvalDataset 实例。
            kb_ids: 运行级知识库限定（用例自带 kb_ids 时优先用例级）。
            with_generation: 是否启用生成 + Judge 评分（False 时只测检索指标）。

        Returns:
            EvalRunResult 汇总结果。
        """
        run_id = str(uuid.uuid4())
        case_results: list[EvalCaseResult] = []

        # 数据集可能传入 list[EvalCase] 或 EvalDataset，统一取迭代器
        cases = list(dataset) if not hasattr(dataset, "cases") else dataset.cases

        if self.engine is None:
            log.warning("eval_runner.engine_none", msg="engine 未注入，检索指标将降级为 0")

        for case in cases:
            case_result = await self._eval_case(case, kb_ids, with_generation)
            case_results.append(case_result)

        # 汇总
        total = len(case_results)
        valid = [c for c in case_results if c.error is None]
        n = len(valid) if valid else (total if total else 1)

        avg_recall = sum(c.recall_at_5 for c in valid) / n if valid else 0.0
        avg_mrr_val = sum(c.mrr for c in valid) / n if valid else 0.0
        avg_ndcg = sum(c.ndcg_at_5 for c in valid) / n if valid else 0.0

        # Judge 评分均值：仅统计有 judge_scores 的用例
        judged = [c for c in valid if c.judge_scores is not None]
        avg_judge = 0.0
        if judged:
            scores = [
                float(c.judge_scores.get("total_score", 0.0))  # type: ignore[union-attr]
                for c in judged
            ]
            avg_judge = sum(scores) / len(scores)

        # RAGAS 指标均值：仅统计有 ragas_scores 的用例
        ragas_list = [c for c in valid if c.ragas_scores is not None]
        avg_ragas: dict[str, float] = {}
        if ragas_list:
            ragas_keys = {"faithfulness", "answer_relevancy", "context_precision", "context_recall"}
            for key in ragas_keys:
                values = [
                    float(c.ragas_scores.get(key, 0.0))  # type: ignore[union-attr]
                    for c in ragas_list
                ]
                avg_ragas[key] = sum(values) / len(values) if values else 0.0

        passed = sum(1 for c in case_results if c.passed)

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
            result = await self._eval_case_inner(case, run_kb_ids, with_generation)
            try:
                collected = recorder.collect()
                result.spans = [s.to_dict() for s in collected]
                # 上下文质量评分（§7.3）：由 Span 证据聚合 ContextTraceRecord 后计算
                context_expect = getattr(case, "context_expect", None)
                if context_expect:
                    from app.eval.context_metrics import compute_context_metrics
                    from app.eval.context_trace import ContextTraceRecord

                    trace = ContextTraceRecord.from_spans(collected)
                    result.context_metrics = compute_context_metrics(
                        trace, context_expect
                    )
            except Exception as exc:  # pragma: no cover - 防御性降级
                log.warning("eval_runner.span_collect_error", error=str(exc))
            return result

    async def _eval_case_inner(
        self,
        case: Any,
        run_kb_ids: list[str] | None,
        with_generation: bool,
    ) -> EvalCaseResult:
        """评测单条用例的实际执行体（由 _eval_case 包裹 span 收集）。"""
        query: str = getattr(case, "query", "")
        expected_doc_ids: list[str] = list(getattr(case, "expected_doc_ids", []))
        # 用例级 kb_ids 优先于运行级
        case_kb_ids = getattr(case, "kb_ids", None)
        kb_ids_for_case = case_kb_ids if case_kb_ids is not None else run_kb_ids

        retrieved_doc_ids: list[str] = []
        retrieved_docs: list[dict[str, Any]] = []
        error: str | None = None

        # 1. 检索（调用 engine._retrieve）
        if self.engine is not None:
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
                retrieve_fn = getattr(self.engine, "_retrieve", None)
                if retrieve_fn is not None:
                    await retrieve_fn(state, kb_ids_for_case)
                    retrieved_docs = list(state.get("retrieved_docs", []) or [])
                    retrieved_doc_ids = [
                        str(d.get("doc_id"))
                        for d in retrieved_docs
                        if isinstance(d, dict) and d.get("doc_id")
                    ]
                else:
                    error = "engine 缺少 _retrieve 方法"
            except Exception as exc:
                log.warning("eval_runner.retrieve_error", query=query[:50], error=str(exc))
                error = f"retrieve_error: {exc}"
        else:
            error = "engine_unavailable"

        # 2. 检索指标（纯数学）
        recall5 = recall_at_k(retrieved_doc_ids, expected_doc_ids, _DEFAULT_K)
        mrr_val = mrr(retrieved_doc_ids, expected_doc_ids)
        ndcg5 = ndcg(retrieved_doc_ids, expected_doc_ids, _DEFAULT_K)

        # 3. 生成 + Judge + RAGAS（可选）
        answer: str | None = None
        judge_scores: dict[str, Any] | None = None
        ragas_scores: dict[str, float] | None = None

        if with_generation:
            answer, judge_scores, gen_error = await self._generate_and_judge(
                query=query,
                kb_ids_for_case=kb_ids_for_case,
                retrieved_docs=retrieved_docs,
                expected_answer=getattr(case, "expected_answer", None),
            )
            if gen_error is not None:
                # 生成失败不覆盖检索错误，但记录到 error（若检索无错）
                if error is None or error == "engine_unavailable":
                    error = gen_error
                else:
                    error = f"{error} | {gen_error}"

            # RAGAS 评估（需要 answer 和 contexts）
            if answer is not None and self.ragas_metrics is not None:
                try:
                    contexts = [
                        str(d.get("content", ""))
                        for d in retrieved_docs
                        if isinstance(d, dict) and d.get("content")
                    ]
                    ragas_scores = await self.ragas_metrics.evaluate(
                        query=query,
                        answer=answer,
                        contexts=contexts,
                        expected_answer=getattr(case, "expected_answer", None),
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
    # 生成 + Judge
    # ------------------------------------------------------------------

    async def _generate_and_judge(
        self,
        query: str,
        kb_ids_for_case: list[str] | None,
        retrieved_docs: list[dict[str, Any]],
        expected_answer: str | None,
    ) -> tuple[str | None, dict[str, Any] | None, str | None]:
        """调用 engine.answer 生成答案，并用 judge_service 评分。

        Returns:
            (answer, judge_scores, error) 三元组。
        """
        if self.engine is None:
            return None, None, None

        # 生成答案
        answer_parts: list[str] = []
        try:
            answer_fn = getattr(self.engine, "answer", None)
            if answer_fn is None:
                return None, None, "engine 缺少 answer 方法"
            async for token in answer_fn(
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
            return None, None, f"generate_error: {exc}"

        answer = "".join(answer_parts) or None

        # Judge 评分
        if answer is None or self.judge_service is None:
            return answer, None, None

        try:
            contexts = [
                str(d.get("content", ""))
                for d in retrieved_docs
                if isinstance(d, dict) and d.get("content")
            ]
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
            return answer, judge_scores, None
        except Exception as exc:
            log.warning("eval_runner.judge_error", query=query[:50], error=str(exc))
            return answer, None, f"judge_error: {exc}"
