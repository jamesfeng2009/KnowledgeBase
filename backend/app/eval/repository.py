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
        metrics = [
            "avg_recall_at_5",
            "avg_mrr",
            "avg_ndcg_at_5",
            "avg_judge_score",
        ]

        result: dict[str, Any] = {"threshold": threshold, "metrics": {}}
        is_regression = False

        for name in metrics:
            cur = float(getattr(current, name, 0.0))
            base = float(getattr(baseline, name, 0.0))
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

        result["is_regression"] = is_regression
        return result

    # ------------------------------------------------------------------
    # 内部转换
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_result(record: EvalResultRecord) -> Any:
        """将 ORM 记录反序列化为 EvalRunResult。"""
        from app.eval.runner import EvalCaseResult, EvalRunResult

        data = record.result_json or {}
        case_results: list[EvalCaseResult] = []
        for cd in data.get("case_results", []):
            case_results.append(
                EvalCaseResult(
                    query=cd.get("query", ""),
                    retrieved_doc_ids=cd.get("retrieved_doc_ids", []),
                    recall_at_5=cd.get("recall_at_5", 0.0),
                    mrr=cd.get("mrr", 0.0),
                    ndcg_at_5=cd.get("ndcg_at_5", 0.0),
                    answer=cd.get("answer"),
                    judge_scores=cd.get("judge_scores"),
                    ragas_scores=cd.get("ragas_scores"),
                    error=cd.get("error"),
                )
            )
        return EvalRunResult(
            case_results=case_results,
            avg_recall_at_5=data.get("avg_recall_at_5", record.avg_recall_at_5),
            avg_mrr=data.get("avg_mrr", record.avg_mrr),
            avg_ndcg_at_5=data.get("avg_ndcg_at_5", record.avg_ndcg_at_5),
            avg_judge_score=data.get("avg_judge_score", record.avg_judge_score),
            avg_ragas=data.get("avg_ragas", {}),
            total=data.get("total", record.total),
            passed=data.get("passed", record.passed),
            evaluated_at=data.get("evaluated_at", record.evaluated_at),
            run_id=record.run_id,
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
