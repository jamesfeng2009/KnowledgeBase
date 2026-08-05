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
    1 — 存在回归（指标下降超阈值）或数据集为空等错误；
    2 — RAG 引擎不可用（P2-3：此前静默降级为全 0 仍退出 0，CI 易误判绿）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


# ======================================================================
# P2-7: 评测裁判 Provider 解析 — 消除「生成模型自评」偏好偏差
# ======================================================================


def _resolve_eval_provider(
    model_id: str | None,
    role: str,
    default_provider: Any | None,
) -> Any | None:
    """解析评测裁判/评分专用 LLM Provider（P2-7）。

    Judge 与 RAGAS 此前直接复用 ``get_llm_provider()``（与生成引擎同一模型），
    引入自我偏好偏差（self-preference bias）——模型倾向于偏好自己生成的答案。

    解析优先级（高 → 低）：
        1. 显式 ``model_id``（CLI ``--judge-model`` / ``--ragas-model``）；
        2. 环境变量 ``EVAL_JUDGE_MODEL`` / ``EVAL_RAGAS_MODEL``；
        3. 回退到 ``default_provider``（即生成模型），并告警偏差风险。

    Args:
        model_id: 显式指定的模型 ID（models.json 中的 id，如 "claude-haiku-4"）。
        role: 角色名（"JUDGE" / "RAGAS"），用于环境变量名与日志。
        default_provider: 解析失败时的回退 Provider（通常为生成模型 Provider）。

    Returns:
        对应模型的 LLMProvider；解析失败时回退到 default_provider。
    """
    env_key = f"EVAL_{role}_MODEL"
    chosen = model_id or os.environ.get(env_key)
    if chosen:
        try:
            from app.llm.factory import get_llm_provider_by_model

            provider = get_llm_provider_by_model(chosen)
            log.info(
                "eval_cli.eval_provider_resolved",
                role=role,
                model_id=chosen,
            )
            return provider
        except Exception as exc:
            log.warning(
                "eval_cli.eval_provider_resolve_failed",
                role=role,
                model_id=chosen,
                error=str(exc),
            )
    # 回退到生成模型 —— 与生成同源，存在自评偏好偏差，显式告警提示运维分离
    log.warning(
        "eval_cli.self_preference_bias",
        role=role,
        msg=(
            f"{role} 复用生成模型 Provider，存在自评偏好偏差风险；"
            f"建议设置 --{role.lower()}-model <model_id> 或环境变量 {env_key}"
        ),
    )
    return default_provider


def _build_judge_with_model(judge_model: str | None) -> Any | None:
    """P2-7: 构建可指定裁判模型的 LLMJudgeService。"""
    try:
        from app.observability.llm_judge import LLMJudgeService
        from app.llm.factory import get_llm_provider

        default = get_llm_provider()
        provider = _resolve_eval_provider(judge_model, "JUDGE", default)
        return LLMJudgeService(provider)
    except Exception as exc:
        log.warning("eval_cli.build_judge_failed", error=str(exc))
        return None


def _build_ragas_with_model(ragas_model: str | None) -> Any | None:
    """P2-7: 构建可指定评分模型的 RagasMetrics。"""
    try:
        from app.eval.ragas_metrics import RagasMetrics
        from app.llm.factory import get_llm_provider

        default = get_llm_provider()
        provider = _resolve_eval_provider(ragas_model, "RAGAS", default)
        return RagasMetrics(provider)
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

    # P1-5: 延迟与 token 成本指标（生产级 RAG 的关键质量维度）
    lines.append("  延迟与成本:")
    lines.append(
        f"    avg_latency_ms       : {result.avg_latency_ms:.2f}\n"
        f"    p99_latency_ms       : {result.p99_latency_ms:.2f}\n"
        f"    total_tokens         : {result.total_tokens}\n"
        f"    avg_total_tokens     : {result.avg_total_tokens:.2f}"
    )

    # P1-4: 工具选择准确度汇总（标注式评测维度）
    if result.tool_selection_summary:
        ts = result.tool_selection_summary
        lines.append("  工具选择准确度:")
        lines.append(
            f"    tool_selection_cases : {ts.get('tool_selection_case_count', 0)}\n"
            f"    avg_tool_precision   : {ts.get('avg_tool_precision', 0.0):.4f}\n"
            f"    avg_tool_recall      : {ts.get('avg_tool_recall', 0.0):.4f}\n"
            f"    avg_tool_f1          : {ts.get('avg_tool_f1', 0.0):.4f}\n"
            f"    tool_pass_rate       : {ts.get('tool_selection_pass_rate', 0.0):.4f}"
        )

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


def _fmt_opt(val: float | None) -> str:
    """格式化可选数值（None 显示为 N/A，P0-2 metric_drops 展示用）。"""
    if val is None:
        return "N/A"
    return f"{val:.2f}"


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

    # case 级对比（§10.5 gate：个案退化也算回归）
    case_diffs = comparison.get("case_diffs") or []
    regressed_cases = [d for d in case_diffs if d.get("regressed")]
    if regressed_cases:
        lines.append(
            f"case 级回归: {len(regressed_cases)} 条用例退化"
        )
        for d in regressed_cases:
            detail = (
                f"  [{d['change']}] {d['query'][:40]}  "
                f"recall {d.get('recall_baseline', 0.0):.2f}"
                f"→{d.get('recall_current', 0.0):.2f}"
            )
            # P0-2: 展示 recall 之外的其他指标退化明细（MRR/NDCG/Judge/RAGAS）
            metric_drops = d.get("metric_drops") or []
            extra_drops = [m for m in metric_drops if m.get("metric") != "recall_at_5"]
            if extra_drops:
                drop_strs = [
                    f"{m['metric']} {_fmt_opt(m.get('baseline'))}"
                    f"→{_fmt_opt(m.get('current'))}(-{m['relative_drop']:.0%})"
                    for m in extra_drops
                ]
                detail += "  " + " ".join(drop_strs)
            if d.get("error"):
                detail += f"  err: {str(d['error'])[:40]}"
            lines.append(detail)
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
        help=(
            "基线标识：run_id 或 'latest'（DB 基线 → 文件基线）。"
            "提供时与当前结果对比，回归则退出码 1"
        ),
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
    parser.add_argument(
        "--judge-model",
        default=None,
        help=(
            "Judge 裁判模型 ID（models.json 中的 id，如 claude-haiku-4）。"
            "P2-7：消除自评偏差，建议 Judge 与生成模型不同；"
            "未指定时回退环境变量 EVAL_JUDGE_MODEL，再回退生成模型（告警偏差）"
        ),
    )
    parser.add_argument(
        "--ragas-model",
        default=None,
        help=(
            "RAGAS 评分模型 ID（models.json 中的 id）。P2-7：建议与生成模型不同；"
            "未指定时回退环境变量 EVAL_RAGAS_MODEL，再回退生成模型（告警偏差）"
        ),
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
# 文件基线（P1-2：CI 跨 run 持久化 — DB 在 CI 中是临时的，
# 基线通过 eval_baseline_<dataset>.json + actions/cache 跨 run 传递）
# ======================================================================


def _baseline_file_path(dataset_name: str) -> str:
    """文件基线路径（相对当前工作目录，CI 中为 backend/）。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in dataset_name)
    return f"eval_baseline_{safe}.json"


def _write_baseline_file(result: EvalRunResult, dataset_name: str) -> None:
    """将基线结果写入文件（best-effort，失败仅告警）。"""
    path = _baseline_file_path(dataset_name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        log.info("eval_cli.baseline_file_written", path=path)
    except Exception as exc:
        log.warning("eval_cli.baseline_file_error", path=path, error=str(exc))


async def _load_baseline(
    repo: EvalRepository, dataset_name: str, baseline_arg: str
) -> Any | None:
    """按优先级加载基线：指定 run_id → DB 基线 → 文件基线（P1-2）。

    ``baseline_arg == "latest"`` 时跳过 run_id 查询，直接取
    「DB 基线 → 文件基线」。文件基线使 CI（临时 DB）也能跨 run 对比。
    """
    if baseline_arg and baseline_arg != "latest":
        baseline = await repo.get_by_run_id(baseline_arg)
        if baseline is not None:
            return baseline
        log.warning(
            "eval_cli.baseline_run_id_miss",
            run_id=baseline_arg,
            msg="回退到 latest 基线",
        )

    baseline = await repo.get_baseline(dataset_name)
    if baseline is not None:
        return baseline

    path = _baseline_file_path(dataset_name)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            log.info("eval_cli.baseline_file_loaded", path=path)
            return EvalRepository.result_from_dict(data)
        except Exception as exc:
            log.warning("eval_cli.baseline_file_load_error", path=path, error=str(exc))
    return None


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
    # P2-7: Judge / RAGAS 使用独立模型（CLI / 环境变量），消除自评偏差
    judge = _build_judge_with_model(args.judge_model) if with_generation else None
    ragas = _build_ragas_with_model(args.ragas_model) if with_generation else None

    if engine is None:
        # P2-3：引擎不可用时指标必然全 0，退出码 0 会让 CI 误判绿。
        # 明确以退出码 2 失败，而非输出一份无意义的全 0 报告。
        print(
            "[ERROR] RAG 引擎不可用，无法产出有效评测指标，退出码 2",
            file=sys.stderr,
        )
        return 2

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
        # P1-2：同步写文件基线，供 CI（临时 DB）跨 run 对比
        _write_baseline_file(result, dataset_name)

    # 6. 基线对比
    is_regression = False
    if args.baseline:
        baseline = asyncio.run(_load_baseline(repo, dataset_name, args.baseline))
        if baseline is None:
            print(
                f"[WARN] 未找到基线 (baseline={args.baseline})，跳过对比",
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
