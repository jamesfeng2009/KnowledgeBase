"""
离线评测结果持久化与回归基线 — 单一职责：评测结果的存取与回归检测。

包含：
    - EvalResultRecord ORM 模型：持久化每次评测运行的汇总指标与完整结果 JSON；
    - EvalRepository：保存 / 查询 / 设置基线 / 回归对比。

设计要点：
    - 参考项目已有 ORM 模式（UUIDMixin + TimestampMixin + Base）；
    - 数据库不可用时优雅降级（返回 None / 空列表 / 仅返回 run_id 不持久化），
      参考 retriever / quality_guard 的 try/except 降级模式；
    - compare_with_baseline 为纯函数，不依赖数据库，按 EVAL_REGRESSION_THRESHOLD
      判定各指标是否回归（相对下降超阈值即视为回归）。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

from sqlalchemy import Boolean, Float, Integer, String, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.models.base import Base, TimestampMixin, UUIDMixin
from app.utils.logger import get_logger

log = get_logger(__name__)

# 延迟导入数据库会话工厂 — 不可用时降级为 None
try:
    from app.database import async_session_factory as _session_factory
except Exception:  # pragma: no cover - 仅在数据库模块异常时触发
    _session_factory = None  # type: ignore[assignment]


class EvalResultRecord(UUIDMixin, TimestampMixin, Base):
    """评测结果记录表 — 持久化每次离线评测运行的汇总指标。

    字段说明：
    - id：主键 UUID（UUIDMixin）；
    - run_id：业务运行 ID（UUID 字符串），便于跨服务引用，唯一索引；
    - result_json：完整 EvalRunResult 的 JSON 快照（含各用例明细）；
    - is_baseline：是否为该数据集的回归基线（同一数据集仅一条 baseline）。
    """

    __tablename__ = "eval_results"

    run_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        nullable=False,
        comment="运行 ID（UUID 字符串）",
    )
    dataset_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True, comment="数据集名称"
    )
    evaluated_at: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="评测时间（ISO 字符串）"
    )
    avg_recall_at_5: Mapped[float] = mapped_column(
        Float, default=0.0, comment="平均 Recall@5"
    )
    avg_mrr: Mapped[float] = mapped_column(
        Float, default=0.0, comment="平均 MRR"
    )
    avg_ndcg_at_5: Mapped[float] = mapped_column(
        Float, default=0.0, comment="平均 NDCG@5"
    )
    avg_judge_score: Mapped[float] = mapped_column(
        Float, default=0.0, comment="平均 Judge 总分"
    )
    total: Mapped[int] = mapped_column(
        Integer, default=0, comment="用例总数"
    )
    passed: Mapped[int] = mapped_column(
        Integer, default=0, comment="通过用例数"
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="完整评测结果 JSON 快照"
    )
    is_baseline: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否为回归基线"
    )


class EvalRepository:
    """评测结果仓储 — 保存 / 查询 / 基线管理 / 回归对比。

    使用方式::

        repo = EvalRepository()              # 自建会话（自动提交）
        repo = EvalRepository(session=db)     # 注入会话（调用方管理事务）

    数据库不可用时所有写操作返回降级值（run_id 仍返回但不持久化），
    读操作返回 None / 空列表，不抛异常。
    """

    def __init__(self, session: Any | None = None) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # 会话上下文
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _session_ctx(self) -> AsyncIterator[Any | None]:
        """获取数据库会话上下文。

        - 注入会话：直接 yield，由调用方管理事务（异常向上抛出）；
        - 自建会话：从全局工厂创建，自动提交/回滚，异常吞掉以优雅降级；
        - 工厂不可用：yield None。
        """
        if self._session is not None:
            yield self._session
            return

        if _session_factory is None:
            yield None
            return

        try:
            async with _session_factory() as session:
                yield session
                await session.commit()
        except Exception as exc:
            log.warning("eval_repo.session_error", error=str(exc))
            # 吞掉异常以实现优雅降级

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def save(
        self,
        result: Any,
        dataset_name: str,
        is_baseline: bool = False,
    ) -> str:
        """保存评测结果，返回 run_id。

        Args:
            result: EvalRunResult 实例。
            dataset_name: 数据集名称。
            is_baseline: 是否设为基线（True 时清除该数据集旧基线）。

        Returns:
            run_id（UUID 字符串）。数据库不可用时仍返回 run_id，但不持久化。
        """
        run_id = getattr(result, "run_id", "") or str(uuid.uuid4())
        # 确保 result.run_id 与返回值一致
        try:
            setattr(result, "run_id", run_id)
        except Exception:  # pragma: no cover
            pass

        result_dict = result.to_dict() if hasattr(result, "to_dict") else {}

        try:
            async with self._session_ctx() as session:
                if session is None:
                    log.warning("eval_repo.save_degraded", run_id=run_id)
                    return run_id

                # 设为基线时，先清除该数据集的旧基线标记
                if is_baseline:
                    await session.execute(
                        update(EvalResultRecord)
                        .where(EvalResultRecord.dataset_name == dataset_name)
                        .where(EvalResultRecord.is_baseline.is_(True))
                        .values(is_baseline=False)
                    )

                record = EvalResultRecord(
                    run_id=run_id,
                    dataset_name=dataset_name,
                    evaluated_at=getattr(result, "evaluated_at", "")
                    or datetime.utcnow().isoformat(),
                    avg_recall_at_5=float(getattr(result, "avg_recall_at_5", 0.0)),
                    avg_mrr=float(getattr(result, "avg_mrr", 0.0)),
                    avg_ndcg_at_5=float(getattr(result, "avg_ndcg_at_5", 0.0)),
                    avg_judge_score=float(getattr(result, "avg_judge_score", 0.0)),
                    total=int(getattr(result, "total", 0)),
                    passed=int(getattr(result, "passed", 0)),
                    result_json=result_dict,
                    is_baseline=is_baseline,
                )
                session.add(record)
                await session.flush()
        except Exception as exc:
            log.warning("eval_repo.save_error", run_id=run_id, error=str(exc))

        return run_id

    # ------------------------------------------------------------------
    # 基线管理
    # ------------------------------------------------------------------

    async def get_baseline(self, dataset_name: str) -> Any | None:
        """获取指定数据集的基线评测结果。

        从 result_json 反序列化为 EvalRunResult 返回。
        数据库不可用或无基线时返回 None。
        """
        try:
            async with self._session_ctx() as session:
                if session is None:
                    return None
                from sqlalchemy import select

                stmt = (
                    select(EvalResultRecord)
                    .where(EvalResultRecord.dataset_name == dataset_name)
                    .where(EvalResultRecord.is_baseline.is_(True))
                    .order_by(EvalResultRecord.created_at.desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalars().first()
                if row is None:
                    return None
                return self._record_to_result(row)
        except Exception as exc:
            log.warning("eval_repo.get_baseline_error", error=str(exc))
            return None

    async def get_by_run_id(self, run_id: str) -> Any | None:
        """按 run_id 查询单次评测结果。

        数据库不可用或未找到时返回 None。
        """
        try:
            async with self._session_ctx() as session:
                if session is None:
                    return None
                from sqlalchemy import select

                stmt = select(EvalResultRecord).where(
                    EvalResultRecord.run_id == run_id
                )
                row = (await session.execute(stmt)).scalars().first()
                if row is None:
                    return None
                return self._record_to_result(row)
        except Exception as exc:
            log.warning("eval_repo.get_by_run_id_error", run_id=run_id, error=str(exc))
            return None

    async def set_baseline(self, run_id: str) -> None:
        """将某次评测结果设为基线（清除同数据集旧基线）。

        数据库不可用时静默降级。
        """
        try:
            async with self._session_ctx() as session:
                if session is None:
                    return
                from sqlalchemy import select

                # 查找目标记录，确定 dataset_name
                stmt = select(EvalResultRecord).where(
                    EvalResultRecord.run_id == run_id
                )
                target = (await session.execute(stmt)).scalars().first()
                if target is None:
                    log.warning("eval_repo.set_baseline_not_found", run_id=run_id)
                    return

                # 清除同数据集旧基线
                await session.execute(
                    update(EvalResultRecord)
                    .where(EvalResultRecord.dataset_name == target.dataset_name)
                    .where(EvalResultRecord.is_baseline.is_(True))
                    .values(is_baseline=False)
                )
                target.is_baseline = True
                await session.flush()
        except Exception as exc:
            log.warning("eval_repo.set_baseline_error", run_id=run_id, error=str(exc))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_results(
        self, dataset_name: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """列出某数据集的历史评测结果（按时间倒序）。

        数据库不可用时返回空列表。
        """
        try:
            async with self._session_ctx() as session:
                if session is None:
                    return []
                from sqlalchemy import select

                stmt = (
                    select(EvalResultRecord)
                    .where(EvalResultRecord.dataset_name == dataset_name)
                    .order_by(EvalResultRecord.created_at.desc())
                    .limit(limit)
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [self._record_to_summary(r) for r in rows]
        except Exception as exc:
            log.warning("eval_repo.list_results_error", error=str(exc))
            return []

    # ------------------------------------------------------------------
    # 回归对比（纯函数，不依赖数据库）
    # ------------------------------------------------------------------

    @staticmethod
    def compare_with_baseline(
        current: Any, baseline: Any
    ) -> dict[str, Any]:
        """对比当前结果与基线，返回各指标 delta 与是否回归。

        回归判定：某指标相对基线下降比例超过 EVAL_REGRESSION_THRESHOLD 即视为回归。
        基线为 0 时，当前值不为正不视为回归（无法计算相对下降）。

        Args:
            current: 当前 EvalRunResult。
            baseline: 基线 EvalRunResult。

        Returns:
            对比结果字典，含各指标 current/baseline/delta/regressed 与总体 is_regression。
        """
        threshold = float(get_settings().EVAL_REGRESSION_THRESHOLD)
        # P2-2 量纲统一：avg_judge_score 为 0-5 分制，其余指标为 0-1 分制，
        # 混在同一阈值下做回归判定会让 Judge 维度的波动被放大解读。
        # 对比时统一归一到 0-1（相对下降比例本身无量纲，此处同时修正
        # 展示值，使对比表中所有指标同量纲可比）。
        metrics = [
            ("avg_recall_at_5", 1.0),
            ("avg_mrr", 1.0),
            ("avg_ndcg_at_5", 1.0),
            ("avg_judge_score", 5.0),
        ]

        result: dict[str, Any] = {"threshold": threshold, "metrics": {}}
        is_regression = False

        for name, scale in metrics:
            cur = float(getattr(current, name, 0.0)) / scale
            base = float(getattr(baseline, name, 0.0)) / scale
            delta = cur - base
            # 相对下降比例
            if base > 0:
                relative_drop = (base - cur) / base
            else:
                # 基线为 0：当前非正不视为回归，当前为正视为改善
                relative_drop = 0.0
            regressed = relative_drop > threshold
            if regressed:
                is_regression = True
            result["metrics"][name] = {
                "current": cur,
                "baseline": base,
                "delta": delta,
                "relative_drop": relative_drop,
                "regressed": regressed,
            }

        # RAGAS 指标回归检测
        cur_ragas = getattr(current, "avg_ragas", {}) or {}
        base_ragas = getattr(baseline, "avg_ragas", {}) or {}
        for ragas_key in ("faithfulness", "answer_relevancy", "context_precision", "context_recall"):
            cur_val = float(cur_ragas.get(ragas_key, 0.0))
            base_val = float(base_ragas.get(ragas_key, 0.0))
            delta = cur_val - base_val
            if base_val > 0:
                relative_drop = (base_val - cur_val) / base_val
            else:
                relative_drop = 0.0
            regressed = relative_drop > threshold
            if regressed:
                is_regression = True
            result["metrics"][f"ragas_{ragas_key}"] = {
                "current": cur_val,
                "baseline": base_val,
                "delta": delta,
                "relative_drop": relative_drop,
                "regressed": regressed,
            }

        # P1-5: 延迟与 token 成本回归检测 —— 与质量指标方向相反（升高即回归）
        # P99 延迟与总 token 成本是生产级 RAG 的关键质量维度，恶化超阈值即回归。
        cost_metrics_higher_worse = ("p99_latency_ms", "avg_latency_ms", "total_tokens")
        for name in cost_metrics_higher_worse:
            cur = float(getattr(current, name, 0.0))
            base = float(getattr(baseline, name, 0.0))
            delta = cur - base
            # 相对上升比例（升高即恶化）
            if base > 0:
                relative_increase = (cur - base) / base
            else:
                # 基线为 0：当前非正不视为回归，当前为正视为新增成本（不判回归
                # —— 首次引入成本不应直接判退，避免基线为 0 时的误报）
                relative_increase = 0.0
            regressed = relative_increase > threshold
            if regressed:
                is_regression = True
            result["metrics"][name] = {
                "current": cur,
                "baseline": base,
                "delta": delta,
                "relative_drop": relative_increase,  # 复用字段名，语义为"相对恶化比例"
                "regressed": regressed,
            }

        # case 级回归对比（评测.md §10.5 gate：均值不变但个案退化也算回归）
        case_diffs, case_regressed = EvalRepository._compare_cases(
            current, baseline, threshold
        )
        result["case_diffs"] = case_diffs
        result["regressed_case_count"] = sum(
            1 for d in case_diffs if d.get("regressed")
        )
        if case_regressed:
            is_regression = True

        # P2-2: 数据集版本指纹校验 — 两侧指纹均存在且不一致时给出警示，
        # 此时对比结果参考价值下降（数据集已变更），但不直接判回归。
        cur_ver = str(getattr(current, "dataset_version", "") or "")
        base_ver = str(getattr(baseline, "dataset_version", "") or "")
        if cur_ver and base_ver and cur_ver != base_ver:
            result["dataset_version_mismatch"] = True
            result["dataset_version"] = {"current": cur_ver, "baseline": base_ver}
            log.warning(
                "eval_repo.dataset_version_mismatch",
                current=cur_ver,
                baseline=base_ver,
            )
        else:
            result["dataset_version_mismatch"] = False

        result["is_regression"] = is_regression
        return result

    # ------------------------------------------------------------------
    # case 级对比（纯函数）
    # ------------------------------------------------------------------

    @staticmethod
    def _compare_cases(
        current: Any, baseline: Any, threshold: float
    ) -> tuple[list[dict[str, Any]], bool]:
        """按 case_id（优先）或 query 匹配对比每个 case，检测 pass→fail 与指标个案退化。

        P2-2：匹配键取 ``case_id or query`` —— 数据集为重复 query 的用例
        标注 case_id 后不再互相覆盖；未标注 case_id 的旧数据保持按 query
        匹配（向后兼容）。

        P0-2：扩展个案退化检测维度 —— 除 recall_at_5 外，新增 MRR / NDCG@5 /
        Judge total_score / RAGAS 四项指标的相对下降检测。任一可计算指标
        相对下降超阈值即标记该 case 退化；指标从可计算退化为不可计算
        （基线有值而当前为 None）视为完全退化（relative_drop=1.0）。

        回归判定（满足其一）：
            1. pass→fail：基线无错误而当前出现错误；
            2. 任一可计算指标相对下降超阈值。

        Returns:
            (case_diffs, any_regressed) 二元组。case_diffs 中 change 取值：
            ok / pass→fail / metric_drop / new / missing。
            触发退化的指标明细列在 ``metric_drops`` 字段（list[dict]）。
        """

        def _case_key(c: Any) -> str:
            return str(getattr(c, "case_id", "") or getattr(c, "query", ""))

        def _get_nested(obj: Any, *path: str) -> float | None:
            """沿属性/键路径取数值，任一环节缺失或非数值返回 None。"""
            cur: Any = obj
            for p in path:
                if cur is None:
                    return None
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    cur = getattr(cur, p, None)
            if cur is None:
                return None
            try:
                return float(cur)
            except (TypeError, ValueError):
                return None

        def _relative_drop(base_val: float | None, cur_val: float | None) -> float | None:
            """计算相对下降（higher is better）。

            - base>0 且 cur 为数值：返回 (base-cur)/base；
            - base>0 且 cur 为 None（指标从可计算退化为不可计算）：返回 1.0；
            - base 为 None 或 base<=0：返回 None（不参与退化判定）。
            """
            if base_val is None or base_val <= 0:
                return None
            if cur_val is None:
                return 1.0
            return (base_val - cur_val) / base_val

        # P0-2: 待检测指标（检索层 mrr/ndcg + 生成层 judge + RAGAS 四项）
        # recall_at_5 单独保留显式字段（向后兼容），其余走通用路径。
        metrics_to_check: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("mrr", ("mrr",)),
            ("ndcg_at_5", ("ndcg_at_5",)),
            ("judge_total_score", ("judge_scores", "total_score")),
            ("ragas_faithfulness", ("ragas_scores", "faithfulness")),
            ("ragas_answer_relevancy", ("ragas_scores", "answer_relevancy")),
            ("ragas_context_precision", ("ragas_scores", "context_precision")),
            ("ragas_context_recall", ("ragas_scores", "context_recall")),
        )

        cur_cases = {
            _case_key(c): c
            for c in (getattr(current, "case_results", None) or [])
        }
        base_cases = {
            _case_key(c): c
            for c in (getattr(baseline, "case_results", None) or [])
        }

        case_diffs: list[dict[str, Any]] = []
        any_regressed = False

        for key, cur in cur_cases.items():
            base = base_cases.get(key)
            if base is None:
                case_diffs.append(
                    {"query": getattr(cur, "query", key), "change": "new", "regressed": False}
                )
                continue

            cur_err = getattr(cur, "error", None)
            base_err = getattr(base, "error", None)
            pass_to_fail = base_err is None and cur_err is not None

            # recall_at_5 相对下降（保留原有显式字段，向后兼容）
            cur_r = float(getattr(cur, "recall_at_5", 0.0))
            base_r = float(getattr(base, "recall_at_5", 0.0))
            recall_drop = (base_r - cur_r) / base_r if base_r > 0 else 0.0

            # P0-2: 汇总所有触发阈值的指标退化明细
            metric_drops: list[dict[str, Any]] = []
            if recall_drop > threshold:
                metric_drops.append({
                    "metric": "recall_at_5",
                    "current": round(cur_r, 4),
                    "baseline": round(base_r, 4),
                    "relative_drop": round(recall_drop, 4),
                })

            for metric_name, path in metrics_to_check:
                base_val = _get_nested(base, *path)
                cur_val = _get_nested(cur, *path)
                drop = _relative_drop(base_val, cur_val)
                if drop is not None and drop > threshold:
                    metric_drops.append({
                        "metric": metric_name,
                        "current": round(cur_val, 4) if cur_val is not None else None,
                        "baseline": round(base_val, 4) if base_val is not None else None,
                        "relative_drop": round(drop, 4),
                    })

            metric_dropped = len(metric_drops) > 0
            regressed = pass_to_fail or metric_dropped
            if regressed:
                any_regressed = True

            if pass_to_fail:
                change = "pass→fail"
            elif metric_dropped:
                change = "metric_drop"
            else:
                change = "ok"

            diff: dict[str, Any] = {
                "query": getattr(cur, "query", key),
                "change": change,
                "regressed": regressed,
                "recall_current": round(cur_r, 4),
                "recall_baseline": round(base_r, 4),
                "recall_relative_drop": round(recall_drop, 4),
                "error": cur_err,
            }
            if metric_drops:
                diff["metric_drops"] = metric_drops
            case_diffs.append(diff)

        # 基线有而当前没有的 case（数据集变更信号，仅提示不算回归）
        for key in base_cases:
            if key not in cur_cases:
                case_diffs.append(
                    {
                        "query": getattr(base_cases[key], "query", key),
                        "change": "missing",
                        "regressed": False,
                    }
                )

        return case_diffs, any_regressed

    # ------------------------------------------------------------------
    # 内部转换
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_result(record: EvalResultRecord) -> Any:
        """将 ORM 记录反序列化为 EvalRunResult。"""
        data = record.result_json or {}
        return EvalRepository.result_from_dict(
            data,
            run_id=record.run_id,
            defaults={
                "avg_recall_at_5": record.avg_recall_at_5,
                "avg_mrr": record.avg_mrr,
                "avg_ndcg_at_5": record.avg_ndcg_at_5,
                "avg_judge_score": record.avg_judge_score,
                "total": record.total,
                "passed": record.passed,
                "evaluated_at": record.evaluated_at,
            },
        )

    @staticmethod
    def result_from_dict(
        data: dict[str, Any],
        run_id: str = "",
        defaults: dict[str, Any] | None = None,
    ) -> Any:
        """从 result_json 字典反序列化为 EvalRunResult。

        P1-2：文件基线（eval_baseline_<dataset>.json，CI 跨 run 缓存）
        与 DB 基线共用同一反序列化路径，保证字段还原一致。

        Args:
            data: ``EvalRunResult.to_dict()`` 产出的字典。
            run_id: 记录级 run_id（data 内 run_id 优先）。
            defaults: 顶层字段缺省值（DB 记录列回退用，文件基线不需要）。
        """
        from app.eval.runner import EvalCaseResult, EvalRunResult

        defaults = defaults or {}
        case_results: list[EvalCaseResult] = []
        for cd in data.get("case_results", []):
            case_results.append(
                EvalCaseResult(
                    query=cd.get("query", ""),
                    case_id=cd.get("case_id", ""),
                    retrieved_doc_ids=cd.get("retrieved_doc_ids", []),
                    recall_at_5=cd.get("recall_at_5", 0.0),
                    mrr=cd.get("mrr", 0.0),
                    ndcg_at_5=cd.get("ndcg_at_5", 0.0),
                    answer=cd.get("answer"),
                    judge_scores=cd.get("judge_scores"),
                    ragas_scores=cd.get("ragas_scores"),
                    error=cd.get("error"),
                    spans=cd.get("spans", []),
                    rule_scores=cd.get("rule_scores"),
                    context_metrics=cd.get("context_metrics"),
                    latency_ms=cd.get("latency_ms"),
                    token_usage=cd.get("token_usage"),
                    iterations=cd.get("iterations"),
                    compression_metrics=cd.get("compression_metrics"),
                )
            )
        return EvalRunResult(
            case_results=case_results,
            avg_recall_at_5=data.get(
                "avg_recall_at_5", defaults.get("avg_recall_at_5", 0.0)
            ),
            avg_mrr=data.get("avg_mrr", defaults.get("avg_mrr", 0.0)),
            avg_ndcg_at_5=data.get("avg_ndcg_at_5", defaults.get("avg_ndcg_at_5", 0.0)),
            avg_judge_score=data.get(
                "avg_judge_score", defaults.get("avg_judge_score", 0.0)
            ),
            avg_ragas=data.get("avg_ragas", {}),
            total=data.get("total", defaults.get("total", 0)),
            passed=data.get("passed", defaults.get("passed", 0)),
            evaluated_at=data.get("evaluated_at", defaults.get("evaluated_at", "")),
            run_id=data.get("run_id", "") or run_id,
            # P2-2: 还原迭代上限与数据集指纹（此前反序列化静默丢失）
            max_iterations=data.get("max_iterations", 5),
            dataset_version=data.get("dataset_version", ""),
            # P1-5: 还原延迟与 token 成本聚合（基线对比时回归检测需要）
            avg_latency_ms=data.get("avg_latency_ms", 0.0),
            p99_latency_ms=data.get("p99_latency_ms", 0.0),
            total_tokens=data.get("total_tokens", 0),
            avg_total_tokens=data.get("avg_total_tokens", 0.0),
        )

    @staticmethod
    def _record_to_summary(record: EvalResultRecord) -> dict[str, Any]:
        """将 ORM 记录转为摘要字典（不含完整 case_results）。"""
        return {
            "run_id": record.run_id,
            "dataset_name": record.dataset_name,
            "evaluated_at": record.evaluated_at,
            "avg_recall_at_5": record.avg_recall_at_5,
            "avg_mrr": record.avg_mrr,
            "avg_ndcg_at_5": record.avg_ndcg_at_5,
            "avg_judge_score": record.avg_judge_score,
            "total": record.total,
            "passed": record.passed,
            "is_baseline": record.is_baseline,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
