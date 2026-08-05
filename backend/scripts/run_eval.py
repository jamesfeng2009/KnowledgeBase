#!/usr/bin/env python
"""
离线评测 CLI 入口 — 加载数据集 → 运行评测 → 对比基线 → 输出报告。

使用示例::

    # 只测检索指标（不调 LLM）
    python scripts/run_eval.py --dataset eval_datasets/sample.jsonl --no-generation

    # 完整评测（检索 + 生成 + Judge）
    python scripts/run_eval.py --dataset eval_datasets/sample.jsonl

    # 限定知识库
    python scripts/run_eval.py --dataset eval_datasets/sample.jsonl --kb-ids kb_finance,kb_hr

    # 与基线对比，回归则退出码 1
    python scripts/run_eval.py --dataset eval_datasets/sample.jsonl --baseline <run_id>

    # 将本次结果设为基线
    python scripts/run_eval.py --dataset eval_datasets/sample.jsonl --set-baseline

退出码：
    0 — 正常完成，无回归；
    1 — 存在回归（指标下降超阈值）或数据集为空等错误。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

# 将 backend 根目录加入 sys.path，使 ``import app`` 可用（脚本可独立运行）
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.eval.dataset import EvalDataset  # noqa: E402
from app.eval.repository import EvalRepository  # noqa: E402
from app.eval.runner import EvalRunner, EvalRunResult  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

log = get_logger(__name__)


# ======================================================================
# 引擎 / Judge 构建（best-effort，失败降级为 None）
# ======================================================================


def _build_engine() -> Any | None:
    """尽力构建 AgenticRAGEngine，任一依赖不可用时返回 None。"""
    try:
        from app.database import async_session_factory
        from app.llm.factory import get_llm_provider
        from app.mcp.client import MCPClient
        from app.mcp.server import KnowledgeBaseMCPServer
        from app.rag.engine import AgenticRAGEngine
        from app.rag.generator import Generator
        from app.rag.reranker import get_reranker
        from app.rag.retriever import HybridRetriever

        llm = get_llm_provider()
        mcp = MCPClient(KnowledgeBaseMCPServer(db_factory=async_session_factory))
        retriever = HybridRetriever()
        reranker = get_reranker()
        generator = Generator(llm)
        return AgenticRAGEngine(
            llm=llm,
            mcp_client=mcp,
            retriever=retriever,
            reranker=reranker,
            generator=generator,
        )
    except Exception as exc:
        log.warning("eval_cli.build_engine_failed", error=str(exc))
        return None


def _build_judge() -> Any | None:
    """尽力构建 LLMJudgeService，不可用时返回 None。"""
    try:
        from app.observability.llm_judge import LLMJudgeService
        from app.llm.factory import get_llm_provider

        return LLMJudgeService(get_llm_provider())
    except Exception as exc:
        log.warning("eval_cli.build_judge_failed", error=str(exc))
        return None


def _build_ragas(llm: Any | None = None) -> Any | None:
    """尽力构建 RagasMetrics 指标计算器，不可用时返回 None。"""
    try:
        from app.eval.ragas_metrics import RagasMetrics

        if llm is None:
            try:
                from app.llm.factory import get_llm_provider
                llm = get_llm_provider()
            except Exception:
                llm = None
        return RagasMetrics(llm)
    except Exception as exc:
        log.warning("eval_cli.build_ragas_failed", error=str(exc))
        return None


# ======================================================================
# 报告格式化
# ======================================================================


def _format_report(result: EvalRunResult, dataset_name: str) -> str:
    """将评测结果格式化为文本表格输出。"""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"离线评测报告  数据集: {dataset_name}  run_id: {result.run_id}")
    lines.append("=" * 72)
    lines.append(
        f"用例总数: {result.total}   通过: {result.passed}   "
        f"评测时间: {result.evaluated_at}"
    )
    lines.append("-" * 72)
    lines.append("汇总指标:")
    lines.append(
        f"  avg_recall_at_5 : {result.avg_recall_at_5:.4f}\n"
        f"  avg_mrr         : {result.avg_mrr:.4f}\n"
        f"  avg_ndcg_at_5   : {result.avg_ndcg_at_5:.4f}\n"
        f"  avg_judge_score : {result.avg_judge_score:.4f}"
    )
    # RAGAS 指标
    if result.avg_ragas:
        lines.append("  RAGAS 指标:")
        for key in ("faithfulness", "answer_relevancy", "context_precision", "context_recall"):
            val = result.avg_ragas.get(key, 0.0)
            lines.append(f"    {key:<22}: {val:.4f}")

    # 统一指标汇总（合并检索 + 生成层）
    try:
        from app.eval.unified_metrics import MetricsAdapter

        unified_list = []
        for c in result.case_results:
            retrieval = {
                "recall_at_5": c.recall_at_5,
                "mrr": c.mrr,
                "ndcg_at_5": c.ndcg_at_5,
            }
            unified_list.append(
                MetricsAdapter.unify_case_result(
                    retrieval_metrics=retrieval,
                    judge_scores=c.judge_scores,
                    ragas_scores=c.ragas_scores,
                )
            )
        if unified_list:
            agg = MetricsAdapter.aggregate_unified(unified_list)
            lines.append("  统一指标汇总 (Unified):")
            for key, val in agg.items():
                lines.append(f"    {key:<22}: {val:.4f}")
    except Exception:
        pass
    lines.append("-" * 72)
    lines.append("用例明细:")
    header = f"{'#':>3}  {'Recall@5':>9}  {'MRR':>6}  {'NDCG@5':>7}  {'Judge':>6}  {'RAGAS':>6}  Query"
    lines.append(header)
    for i, c in enumerate(result.case_results, start=1):
        judge_val = (
            f"{c.judge_scores.get('total_score', 0.0):.2f}"
            if c.judge_scores
            else "-"
        )
        ragas_val = "-"
        if c.ragas_scores:
            # 显示 faithfulness 作为 RAGAS 代表值
            ragas_val = f"{c.ragas_scores.get('faithfulness', 0.0):.2f}"
        err_tag = f"  [err: {c.error}]" if c.error else ""
        lines.append(
            f"{i:>3}  {c.recall_at_5:>9.4f}  {c.mrr:>6.4f}  "
            f"{c.ndcg_at_5:>7.4f}  {judge_val:>6}  {ragas_val:>6}  {c.query[:30]}{err_tag}"
        )
    lines.append("=" * 72)
    return "\n".join(lines)


def _format_comparison(comparison: dict[str, Any]) -> str:
    """格式化基线对比结果。"""
    lines: list[str] = []
    lines.append("-" * 72)
    lines.append(
        f"基线对比  阈值: {comparison.get('threshold', 0.0):.2%}  "
        f"回归: {'是' if comparison.get('is_regression') else '否'}"
    )
    lines.append(
        f"{'指标':<22}{'当前':>10}{'基线':>10}{'delta':>10}{'下降%':>10}{'回归':>8}"
    )
    for name, m in comparison.get("metrics", {}).items():
        lines.append(
            f"{name:<22}{m['current']:>10.4f}{m['baseline']:>10.4f}"
            f"{m['delta']:>10.4f}{m['relative_drop']:>9.2%}{'  是' if m['regressed'] else '  否':>8}"
        )
    lines.append("-" * 72)
    return "\n".join(lines)


# ======================================================================
# 参数解析
# ======================================================================


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="企业知识库离线评测 CLI",
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="评测数据集路径（JSONL 文件或目录）",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="基线 run_id（提供时与当前结果对比，回归则退出码 1）",
    )
    parser.add_argument(
        "--kb-ids",
        default=None,
        help="限定知识库 ID 列表（逗号分隔），如 kb_finance,kb_hr",
    )
    parser.add_argument(
        "--no-generation",
        action="store_true",
        help="只测检索指标，不调用生成与 Judge",
    )
    parser.add_argument(
        "--set-baseline",
        action="store_true",
        help="将本次评测结果设为该数据集的回归基线",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Agent Loop 最大迭代次数（默认 5，最小 1），用于多轮任务级评估",
    )
    return parser.parse_args(argv)


def _derive_dataset_name(dataset_path: str) -> str:
    """从数据集路径派生数据集名称（文件名/目录名，去扩展名）。"""
    cleaned = dataset_path.rstrip(os.sep)
    base = os.path.basename(cleaned)
    if base.lower().endswith(".jsonl"):
        base = base[: -len(".jsonl")]
    return base or "dataset"


# ======================================================================
# 主流程
# ======================================================================


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，返回退出码。"""
    args = _parse_args(argv)

    # 1. 加载数据集
    if os.path.isdir(args.dataset):
        dataset = EvalDataset.load_from_dir(args.dataset)
    else:
        dataset = EvalDataset.load(args.dataset)

    if len(dataset) == 0:
        print(f"[ERROR] 数据集为空或加载失败: {args.dataset}", file=sys.stderr)
        return 1

    dataset_name = _derive_dataset_name(args.dataset)

    # 2. 解析 kb_ids
    kb_ids: list[str] | None = None
    if args.kb_ids:
        kb_ids = [k.strip() for k in args.kb_ids.split(",") if k.strip()]

    with_generation = not args.no_generation

    # 3. 构建引擎 / Judge / RAGAS（best-effort）
    engine = _build_engine()
    judge = _build_judge() if with_generation else None
    ragas = _build_ragas() if with_generation else None

    if engine is None:
        print(
            "[WARN] RAG 引擎不可用，检索指标将降级为 0（仅验证流程）",
            file=sys.stderr,
        )

    # 4. 运行评测
    runner = EvalRunner(
        engine=engine,
        judge_service=judge,
        ragas_metrics=ragas,
        max_iterations=args.max_iterations,
    )
    result = asyncio.run(
        runner.run(dataset, kb_ids=kb_ids, with_generation=with_generation)
    )

    # 5. 持久化（best-effort）—— 始终保存到历史，--set-baseline 时标记为基线
    repo = EvalRepository()
    run_id = asyncio.run(
        repo.save(result, dataset_name, is_baseline=args.set_baseline)
    )
    # 保持 result.run_id 与持久化返回一致
    result.run_id = run_id

    if args.set_baseline:
        print(f"[INFO] 已将本次结果设为基线 run_id={run_id}", file=sys.stderr)

    # 6. 基线对比
    is_regression = False
    if args.baseline:
        baseline = asyncio.run(repo.get_by_run_id(args.baseline))
        if baseline is None:
            # 回退：取该数据集的当前基线
            baseline = asyncio.run(repo.get_baseline(dataset_name))
        if baseline is None:
            print(
                f"[WARN] 未找到基线 (run_id={args.baseline})，跳过对比",
                file=sys.stderr,
            )
        else:
            comparison = EvalRepository.compare_with_baseline(result, baseline)
            print(_format_comparison(comparison))
            is_regression = bool(comparison.get("is_regression"))

    # 7. 输出报告
    print(_format_report(result, dataset_name))

    # 8. 回归则退出码 1
    if is_regression:
        print("[FAIL] 检测到回归，指标下降超过阈值", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
