"""
RAG 检索质量评测服务 — 单一职责：执行检索、计算质量指标、汇总统计。

遵循单一职责：
    - 检索执行：复用生产级 HybridRetriever（向量+全文混合检索），不重造轮子
    - 指标计算：基于标注 ground_truth 与检索排序结果，计算 Recall@K / Precision@K
                / MRR / NDCG@K / MAP（参考 test.md「语义检索流程」一节）
    - 数据管理：数据集 / 查询 / 结果的 CRUD

指标说明（参考 test.md）：
    - Recall@K（topN 召回率）：检索 topK 中命中相关文档的比例（RAG 主指标）
    - Precision@K：检索 topK 中相关文档的占比
    - MRR：第一个相关文档的倒数排名均值
    - NDCG@K：考虑排序位置打折的累计增益（归一化）
    - MAP：平均精度均值，综合考虑召回与顺序（test.md 给出 sklearn 计算示例）

评分对象：以 doc_id 为粒度（chunk 重新索引时 chunk_id 不稳定，doc_id 稳定且用户可感知）。
"""

from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_eval import (
    RagEvalDataset,
    RagEvalQuery,
    RagEvalResult,
)
from app.rag.retriever import HybridRetriever
from app.services.ai_eval.rag_eval_queries import (
    get_preset_queries,
    get_query_type_summary,
)
from app.utils.logger import get_logger

log = get_logger(__name__)

# 指标计算默认 K 值序列
_DEFAULT_K_VALUES: list[int] = [1, 3, 5]
# NDCG/MAP 固定 K 值（与 Recall@5 对齐，便于横向对比）
_NDCG_K: int = 5


# ======================================================================
# 指标计算 — 纯函数（无副作用，便于单测）
# ======================================================================


def compute_retrieval_metrics(
    ranked_doc_ids: list[str],
    relevant_doc_ids: list[str],
    k_values: list[int] | None = None,
) -> dict:
    """计算单条查询的检索质量指标。

    Args:
        ranked_doc_ids: 检索返回的文档 ID 列表（按相关性降序，已按 doc_id 去重）。
        relevant_doc_ids: 人工标注的相关文档 ID 列表（ground truth）。
        k_values: 需计算 Recall@K / Precision@K 的 K 值列表，默认 [1, 3, 5]。

    Returns:
        指标 dict::

            {
                "recall_at_1", "recall_at_3", "recall_at_5",
                "precision_at_1", "precision_at_3", "precision_at_5",
                "mrr", "ndcg_at_5", "map", "hit",
            }

    指标定义（参考 test.md）：
        - Recall@K = (topK 中命中的相关文档数) / (相关文档总数)
        - Precision@K = (topK 中命中的相关文档数) / K
        - MRR = 1 / (第一个相关文档的排名)；无命中则为 0
        - NDCG@K = DCG@K / IDCG@K，DCG = Σ rel_i / log2(i+1)
        - MAP = (Σ_{命中 k} k/排名_k) / 相关文档总数
        - hit = topK 内是否命中任一相关文档（K 取 k_values 最大值）
    """
    if k_values is None:
        k_values = _DEFAULT_K_VALUES

    relevant_set = {str(d) for d in relevant_doc_ids if d}
    ranked = [str(d) for d in ranked_doc_ids if d]

    metrics: dict[str, float | bool] = {}

    # Recall@K / Precision@K
    for k in k_values:
        topk = ranked[:k]
        hit_count = sum(1 for d in topk if d in relevant_set)
        recall = hit_count / len(relevant_set) if relevant_set else 0.0
        precision = hit_count / k if k > 0 else 0.0
        metrics[f"recall_at_{k}"] = round(recall, 4)
        metrics[f"precision_at_{k}"] = round(precision, 4)

    # MRR — 第一个命中相关文档的倒数排名
    mrr = 0.0
    for i, d in enumerate(ranked, start=1):
        if d in relevant_set:
            mrr = 1.0 / i
            break
    metrics["mrr"] = round(mrr, 4)

    # NDCG@K — 二值相关性（命中=1），位置打折后归一化
    k = _NDCG_K
    dcg = 0.0
    for i, d in enumerate(ranked[:k], start=1):
        if d in relevant_set:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hit_count = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hit_count + 1))
    metrics[f"ndcg_at_{k}"] = round(dcg / idcg, 4) if idcg > 0 else 0.0

    # MAP — 平均精度均值（综合考虑召回与顺序）
    if relevant_set:
        hits = 0
        sum_precision = 0.0
        for i, d in enumerate(ranked, start=1):
            if d in relevant_set:
                hits += 1
                sum_precision += hits / i
        metrics["map"] = round(sum_precision / len(relevant_set), 4)
    else:
        metrics["map"] = 0.0

    # hit — topK（取最大 K）内是否命中
    max_k = max(k_values) if k_values else k
    top_maxk = ranked[:max_k]
    metrics["hit"] = any(d in relevant_set for d in top_maxk)

    return metrics


# ======================================================================
# 服务
# ======================================================================


class RagEvalService:
    """RAG 检索质量评测服务。

    使用方式::

        service = RagEvalService(db)
        dataset = await service.create_dataset(name="检索评测1", user_id=user.id, kb_ids=[...])
        await service.add_query(dataset.id, query="报销流程", ground_truth_doc_ids=[...])
        await service.run_dataset(dataset.id)
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 数据集管理
    # ------------------------------------------------------------------

    async def create_dataset(
        self,
        name: str,
        user_id: uuid.UUID,
        description: str | None = None,
        kb_ids: list[str] | None = None,
        top_k: int = 5,
        tenant_id: uuid.UUID | None = None,
    ) -> RagEvalDataset:
        """创建评测数据集。"""
        dataset = RagEvalDataset(
            name=name,
            description=description,
            kb_ids=kb_ids,
            top_k=top_k,
            created_by=user_id,
            tenant_id=tenant_id,
            status="created",
        )
        self.db.add(dataset)
        await self.db.flush()
        log.info("rag_dataset_created", dataset_id=str(dataset.id), name=name)
        return dataset

    async def get_dataset(self, dataset_id: uuid.UUID) -> RagEvalDataset | None:
        """获取数据集（含软删除过滤）。"""
        dataset = await self.db.get(RagEvalDataset, dataset_id)
        if dataset is None or dataset.deleted_at is not None:
            return None
        return dataset

    async def delete_dataset(self, dataset_id: uuid.UUID) -> bool:
        """软删除数据集。"""
        dataset = await self.get_dataset(dataset_id)
        if dataset is None:
            return False
        dataset.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
        log.info("rag_dataset_deleted", dataset_id=str(dataset_id))
        return True

    # ------------------------------------------------------------------
    # 查询管理
    # ------------------------------------------------------------------

    async def add_query(
        self,
        dataset_id: uuid.UUID,
        query: str,
        ground_truth_doc_ids: list[str],
        query_type: str = "semantic",
        difficulty: str = "medium",
        expected_answer: str | None = None,
        source: str = "custom",
    ) -> RagEvalQuery:
        """添加一条评测查询（含人工标注）。"""
        q = RagEvalQuery(
            dataset_id=dataset_id,
            query=query,
            query_type=query_type,
            difficulty=difficulty,
            ground_truth_doc_ids=ground_truth_doc_ids,
            expected_answer=expected_answer,
            source=source,
        )
        self.db.add(q)
        await self.db.flush()

        # 更新数据集查询数
        dataset = await self.get_dataset(dataset_id)
        if dataset:
            dataset.total_queries = (dataset.total_queries or 0) + 1
            await self.db.flush()

        log.info(
            "rag_query_added",
            dataset_id=str(dataset_id),
            query_id=str(q.id),
            query_type=query_type,
        )
        return q

    async def import_preset_queries(
        self,
        dataset_id: uuid.UUID,
    ) -> list[RagEvalQuery]:
        """导入预置查询模板到指定数据集。

        注意：预置模板不含 ground_truth_doc_ids（文档 ID 为用户私有），
        导入后为待标注状态，用户须从知识库选择相关文档完成标注才能评测。
        """
        presets = get_preset_queries()
        queries: list[RagEvalQuery] = []
        for p in presets:
            q = RagEvalQuery(
                dataset_id=dataset_id,
                query=p["query"],
                query_type=p["query_type"],
                difficulty=p["difficulty"],
                # 预置模板无标注，置空（用户须手动标注）
                ground_truth_doc_ids=None,
                source="preset",
            )
            self.db.add(q)
            queries.append(q)

        await self.db.flush()

        dataset = await self.get_dataset(dataset_id)
        if dataset:
            dataset.total_queries = (dataset.total_queries or 0) + len(queries)
            await self.db.flush()

        log.info(
            "preset_queries_imported",
            dataset_id=str(dataset_id),
            count=len(queries),
        )
        return queries

    async def delete_query(self, query_id: uuid.UUID) -> bool:
        """软删除一条评测查询（项目约束：不允许物理删除数据库数据）。"""
        q = await self.db.get(RagEvalQuery, query_id)
        if q is None or q.deleted_at is not None:
            return False
        dataset_id = q.dataset_id
        q.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()

        # 更新数据集查询数
        dataset = await self.get_dataset(dataset_id)
        if dataset:
            count = await self.db.scalar(
                select(func.count())
                .select_from(RagEvalQuery)
                .where(RagEvalQuery.dataset_id == dataset_id)
                .where(RagEvalQuery.deleted_at.is_(None))
            )
            dataset.total_queries = count or 0
            await self.db.flush()
        return True

    async def list_queries(self, dataset_id: uuid.UUID) -> list[RagEvalQuery]:
        """列出数据集下所有查询（过滤已软删除）。"""
        result = await self.db.execute(
            select(RagEvalQuery)
            .where(RagEvalQuery.dataset_id == dataset_id)
            .where(RagEvalQuery.deleted_at.is_(None))
            .order_by(RagEvalQuery.created_at)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 执行评测
    # ------------------------------------------------------------------

    async def run_dataset(
        self,
        dataset_id: uuid.UUID,
        top_k: int | None = None,
    ) -> dict:
        """执行整个数据集的检索评测。

        对每条已标注查询调用 HybridRetriever 检索，按 doc_id 去重保序后
        计算指标并保存结果，最后回填数据集聚合指标。

        Args:
            dataset_id: 数据集 ID。
            top_k: 检索 top_k（空则用数据集默认值）。

        Returns:
            汇总统计 dict。
        """
        dataset = await self.get_dataset(dataset_id)
        if dataset is None:
            raise ValueError(f"数据集 {dataset_id} 不存在")

        k = top_k or dataset.top_k or 5
        dataset.status = "running"
        await self.db.flush()

        start_time = time.time()
        hit_count = 0
        executed = 0
        # 累加各指标用于求均值
        metric_sums: dict[str, float] = {}

        try:
            queries = await self.list_queries(dataset_id)
            retriever = HybridRetriever()
        except Exception:
            # 前置步骤失败时恢复状态，否则数据集永久卡在 "running"
            dataset.status = "failed"
            await self.db.flush()
            raise
        # 显式取出 kb_ids 传入 _execute_query，避免在 async 上下文
        # 访问 query.dataset 触发懒加载（MissingGreenletError）
        kb_ids = dataset.kb_ids

        log.info(
            "rag_dataset_started",
            dataset_id=str(dataset_id),
            total_queries=len(queries),
            top_k=k,
        )

        try:
            for q in queries:
                # 跳过未标注查询（无 ground_truth 无法计算指标）
                if not q.ground_truth_doc_ids:
                    continue
                try:
                    metrics = await self._execute_query(
                        dataset_id=dataset_id,
                        query=q,
                        retriever=retriever,
                        top_k=k,
                        kb_ids=kb_ids,
                    )
                    executed += 1
                    if metrics.get("hit"):
                        hit_count += 1
                    for key, val in metrics.items():
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            metric_sums[key] = metric_sums.get(key, 0.0) + float(val)
                except Exception as exc:
                    log.error(
                        "rag_query_error",
                        query_id=str(q.id),
                        error=str(exc),
                    )
                    # 记录错误结果
                    self.db.add(RagEvalResult(
                        query_id=q.id,
                        dataset_id=dataset_id,
                        retrieved=[],
                        metrics={},
                        retrieved_count=0,
                        error_message=str(exc),
                        executed_at=datetime.now(timezone.utc),
                    ))
        finally:
            await retriever.close()

        # 聚合指标（按已执行查询数求均值）
        # 置于保护内：聚合/回填异常也不能让状态卡在 running
        try:
            agg_metrics: dict[str, float] = {}
            if executed > 0:
                for key, total in metric_sums.items():
                    agg_metrics[key] = round(total / executed, 4)
            agg_metrics["hit_rate"] = round(hit_count / executed, 4) if executed > 0 else 0.0

            elapsed = int(time.time() - start_time)
            dataset.status = "completed"
            dataset.hit_count = hit_count
            dataset.metrics = agg_metrics
            dataset.duration_seconds = elapsed
        except Exception:
            dataset.status = "failed"
            await self.db.flush()
            raise

        log.info(
            "rag_dataset_completed",
            dataset_id=str(dataset_id),
            executed=executed,
            hit_count=hit_count,
            elapsed=elapsed,
            avg_recall_at_5=agg_metrics.get("recall_at_5"),
            avg_mrr=agg_metrics.get("mrr"),
        )

        return {
            "total": len(queries),
            "executed": executed,
            "hit_count": hit_count,
            "hit_rate": agg_metrics.get("hit_rate", 0.0),
            "avg_recall_at_5": agg_metrics.get("recall_at_5", 0.0),
            "avg_mrr": agg_metrics.get("mrr", 0.0),
            "avg_ndcg_at_5": agg_metrics.get(f"ndcg_at_{_NDCG_K}", 0.0),
            "avg_map": agg_metrics.get("map", 0.0),
            "duration_seconds": elapsed,
            "metrics": agg_metrics,
        }

    async def _execute_query(
        self,
        dataset_id: uuid.UUID,
        query: RagEvalQuery,
        retriever: HybridRetriever,
        top_k: int,
        kb_ids: list[str] | None = None,
    ) -> dict:
        """执行单条查询检索并计算指标，保存结果。返回指标 dict。"""
        start = time.time()

        # 调用生产级混合检索器（kb_ids 由上层显式传入，避免懒加载）
        results = await retriever.search(
            query=query.query,
            kb_ids=kb_ids,
            top_k=top_k,
        )

        response_time_ms = int((time.time() - start) * 1000)

        # 按 doc_id 去重保序（同一文档多个 chunk 只保留首个/最高分）
        seen_doc_ids: set[str] = set()
        retrieved: list[dict] = []
        ranked_doc_ids: list[str] = []
        for idx, r in enumerate(results):
            doc_id = str(r.get("doc_id") or "")
            if not doc_id or doc_id in seen_doc_ids:
                continue
            seen_doc_ids.add(doc_id)
            rank = len(retrieved) + 1
            retrieved.append({
                "doc_id": doc_id,
                "chunk_id": str(r.get("chunk_id") or ""),
                "score": float(r.get("score") or 0.0),
                "rank": rank,
                "source": r.get("source") or "",
                "title": r.get("title") or "",
            })
            ranked_doc_ids.append(doc_id)

        # 计算指标
        ground_truth = query.ground_truth_doc_ids or []
        metrics = compute_retrieval_metrics(
            ranked_doc_ids=ranked_doc_ids,
            relevant_doc_ids=ground_truth,
        )

        # 保存结果（覆盖旧结果）
        existing = await self.db.scalar(
            select(RagEvalResult).where(RagEvalResult.query_id == query.id)
        )
        if existing:
            result = existing
            result.retrieved = retrieved
            result.metrics = metrics
            result.retrieved_count = len(retrieved)
            result.response_time_ms = response_time_ms
            result.error_message = None
            result.executed_at = datetime.now(timezone.utc)
        else:
            result = RagEvalResult(
                query_id=query.id,
                dataset_id=dataset_id,
                retrieved=retrieved,
                metrics=metrics,
                retrieved_count=len(retrieved),
                response_time_ms=response_time_ms,
                executed_at=datetime.now(timezone.utc),
            )
            self.db.add(result)
        await self.db.flush()

        log.info(
            "rag_query_executed",
            query_id=str(query.id),
            retrieved_count=len(retrieved),
            recall_at_5=metrics.get("recall_at_5"),
            mrr=metrics.get("mrr"),
            response_time_ms=response_time_ms,
        )
        return metrics

    # ------------------------------------------------------------------
    # 查询结果
    # ------------------------------------------------------------------

    async def get_dataset_results(
        self,
        dataset_id: uuid.UUID,
    ) -> list[dict]:
        """获取数据集下所有查询的检索结果与指标。"""
        result = await self.db.execute(
            select(RagEvalQuery, RagEvalResult)
            .outerjoin(
                RagEvalResult,
                RagEvalResult.query_id == RagEvalQuery.id,
            )
            .where(RagEvalQuery.dataset_id == dataset_id)
            .order_by(RagEvalQuery.created_at)
        )
        rows = result.all()

        results: list[dict] = []
        for q, res in rows:
            results.append({
                "query_id": str(q.id),
                "query": q.query,
                "query_type": q.query_type,
                "difficulty": q.difficulty,
                "ground_truth_doc_ids": q.ground_truth_doc_ids,
                "retrieved": res.retrieved if res else None,
                "metrics": res.metrics if res else None,
                "retrieved_count": res.retrieved_count if res else 0,
                "response_time_ms": res.response_time_ms if res else 0,
                "error_message": res.error_message if res else None,
                "executed_at": res.executed_at.isoformat() if res and res.executed_at else None,
            })
        return results

    # ------------------------------------------------------------------
    # 全局统计
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        """获取 RAG 评测全局统计。"""
        # 数据集总数
        total_datasets = await self.db.scalar(
            select(func.count())
            .select_from(RagEvalDataset)
            .where(RagEvalDataset.deleted_at.is_(None))
        ) or 0

        # 查询总数
        total_queries = await self.db.scalar(
            select(func.count())
            .select_from(RagEvalQuery)
            .join(
                RagEvalDataset,
                RagEvalDataset.id == RagEvalQuery.dataset_id,
            )
            .where(RagEvalDataset.deleted_at.is_(None))
        ) or 0

        # 已执行结果数 — 排除软删除数据集，与 total_datasets/total_queries 口径一致
        total_executed = await self.db.scalar(
            select(func.count())
            .select_from(RagEvalResult)
            .join(
                RagEvalDataset,
                RagEvalDataset.id == RagEvalResult.dataset_id,
            )
            .where(RagEvalDataset.deleted_at.is_(None))
        ) or 0

        # 聚合各已执行数据集的指标（加权平均）
        ds_result = await self.db.execute(
            select(RagEvalDataset)
            .where(
                RagEvalDataset.deleted_at.is_(None),
                RagEvalDataset.status == "completed",
                RagEvalDataset.metrics.isnot(None),
            )
        )
        completed_datasets = list(ds_result.scalars().all())

        agg_keys = [
            "recall_at_5", "mrr", f"ndcg_at_{_NDCG_K}", "map",
        ]
        sums = {k: 0.0 for k in agg_keys}
        weight_total = 0
        # 按查询类型统计
        type_stats: dict[str, dict] = {}

        for ds in completed_datasets:
            m = ds.metrics or {}
            w = ds.total_queries or 0
            weight_total += w
            for k in agg_keys:
                if k in m:
                    sums[k] += float(m[k]) * w

        # 按查询类型汇总（遍历所有已执行结果）
        all_results = await self.db.execute(
            select(RagEvalQuery, RagEvalResult)
            .outerjoin(
                RagEvalResult,
                RagEvalResult.query_id == RagEvalQuery.id,
            )
            .join(
                RagEvalDataset,
                RagEvalDataset.id == RagEvalQuery.dataset_id,
            )
            .where(RagEvalDataset.deleted_at.is_(None))
        )
        for q, res in all_results.all():
            qt = q.query_type
            if qt not in type_stats:
                type_stats[qt] = {
                    "total": 0, "executed": 0, "hit": 0,
                    "recall_at_5_sum": 0.0, "mrr_sum": 0.0,
                }
            type_stats[qt]["total"] += 1
            if res and res.metrics:
                type_stats[qt]["executed"] += 1
                rm = res.metrics
                if rm.get("hit"):
                    type_stats[qt]["hit"] += 1
                if "recall_at_5" in rm:
                    type_stats[qt]["recall_at_5_sum"] += float(rm["recall_at_5"])
                if "mrr" in rm:
                    type_stats[qt]["mrr_sum"] += float(rm["mrr"])

        # 计算按类型的平均
        by_query_type: dict[str, dict] = {}
        for qt, st in type_stats.items():
            executed = st["executed"]
            by_query_type[qt] = {
                "total": st["total"],
                "executed": executed,
                "hit_rate": round(st["hit"] / executed, 4) if executed else 0.0,
                "avg_recall_at_5": round(st["recall_at_5_sum"] / executed, 4) if executed else 0.0,
                "avg_mrr": round(st["mrr_sum"] / executed, 4) if executed else 0.0,
            }

        return {
            "total_datasets": total_datasets,
            "total_queries": total_queries,
            "total_executed": total_executed,
            "avg_recall_at_5": round(sums["recall_at_5"] / weight_total, 4) if weight_total else 0.0,
            "avg_mrr": round(sums["mrr"] / weight_total, 4) if weight_total else 0.0,
            "avg_ndcg_at_5": round(sums[f"ndcg_at_{_NDCG_K}"] / weight_total, 4) if weight_total else 0.0,
            "avg_map": round(sums["map"] / weight_total, 4) if weight_total else 0.0,
            "by_query_type": by_query_type,
            "preset_query_count": len(get_preset_queries()),
            "preset_query_types": get_query_type_summary(),
        }
