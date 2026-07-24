"""
文档解析评测服务 — 单一职责：执行文档解析、计算解析质量指标、汇总统计。

遵循单一职责：
    - 解析执行：复用生产级 DoclingParser（Docling 统一解析器），下载文档 → 解析为 HTML
    - 指标计算：复用 doc_parse_metrics.compute_parse_metrics（文本/表格/公式/版面四维度）
    - 数据管理：数据集 / 用例 / 结果的 CRUD

两种评测模式（参考 test.md 第六部分）：
    1. 直接提供模式：用例自带 expected_text（标注）+ parsed_text（待评测解析结果）
    2. Docling 端到端模式：用例自带 expected_text + document_id（下载文件后用 Docling 解析）

指标体系（参考 test.md「测试指标金字塔」）：
    文本相似度（编辑距离/CER）+ 表格准确率（单元格匹配）+
    公式准确率（LaTeX 匹配）+ 版面还原度（标题/段落/列表结构）
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_eval import (
    DocParseCase,
    DocParseDataset,
    DocParseResult,
)
from app.services.ai_eval.doc_parse_metrics import compute_parse_metrics
from app.utils.logger import get_logger

log = get_logger(__name__)

# Docling 解析的文件后缀映射
_DOC_SUFFIX_MAP = {
    "pdf": ".pdf", "docx": ".docx", "pptx": ".pptx",
    "xlsx": ".xlsx", "xls": ".xls", "html": ".html", "htm": ".html",
    "md": ".md", "txt": ".txt",
    "png": ".png", "jpg": ".jpg", "jpeg": ".jpeg", "gif": ".gif",
    "webp": ".webp", "tiff": ".tiff", "bmp": ".bmp",
}


def _strip_html_to_text(html: str) -> str:
    """将 HTML 转为纯文本（保留表格 | 结构与换行）。

    Docling 输出 HTML，对比标注前需转纯文本。
    保留 <table> 内 | 分隔，块级标签转换行。
    """
    if not html:
        return ""
    import re
    # 块级标签转换行
    text = re.sub(r"<\s*(/)?\s*(p|div|h[1-6]|li|tr|br)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # 表格单元格转 | 分隔
    text = re.sub(r"<\s*(/)?\s*td[^>]*>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*(/)?\s*th[^>]*>", " | ", text, flags=re.IGNORECASE)
    # 去除其余标签
    text = re.sub(r"<[^>]+>", "", text)
    # HTML 实体
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    # 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class DocParseService:
    """文档解析评测服务。

    使用方式::

        service = DocParseService(db)
        dataset = await service.create_dataset(name="解析评测1", user_id=user.id)
        await service.add_case(dataset.id, title="财报", expected_text="...", parsed_text="...")
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
        tenant_id: uuid.UUID | None = None,
    ) -> DocParseDataset:
        """创建解析评测数据集。"""
        dataset = DocParseDataset(
            name=name,
            description=description,
            created_by=user_id,
            tenant_id=tenant_id,
            status="created",
        )
        self.db.add(dataset)
        await self.db.flush()
        log.info("doc_parse_dataset_created", dataset_id=str(dataset.id), name=name)
        return dataset

    async def get_dataset(self, dataset_id: uuid.UUID) -> DocParseDataset | None:
        """获取数据集（含软删除过滤）。"""
        dataset = await self.db.get(DocParseDataset, dataset_id)
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
        return True

    # ------------------------------------------------------------------
    # 用例管理
    # ------------------------------------------------------------------

    async def add_case(
        self,
        dataset_id: uuid.UUID,
        title: str,
        expected_text: str,
        doc_type: str = "pdf",
        difficulty: str = "medium",
        document_id: uuid.UUID | None = None,
        parsed_text: str | None = None,
        source: str = "custom",
    ) -> DocParseCase:
        """添加一条解析评测用例。"""
        case = DocParseCase(
            dataset_id=dataset_id,
            title=title,
            doc_type=doc_type,
            difficulty=difficulty,
            expected_text=expected_text,
            document_id=document_id,
            parsed_text=parsed_text,
            source=source,
        )
        self.db.add(case)
        await self.db.flush()

        dataset = await self.get_dataset(dataset_id)
        if dataset:
            dataset.total_cases = (dataset.total_cases or 0) + 1
            await self.db.flush()
        return case

    async def delete_case(self, case_id: uuid.UUID) -> bool:
        """删除一条用例。"""
        case = await self.db.get(DocParseCase, case_id)
        if case is None:
            return False
        dataset_id = case.dataset_id
        await self.db.delete(case)
        await self.db.flush()
        dataset = await self.get_dataset(dataset_id)
        if dataset:
            count = await self.db.scalar(
                select(func.count())
                .select_from(DocParseCase)
                .where(DocParseCase.dataset_id == dataset_id)
            )
            dataset.total_cases = count or 0
            await self.db.flush()
        return True

    async def list_cases(self, dataset_id: uuid.UUID) -> list[DocParseCase]:
        """列出数据集下所有用例。"""
        result = await self.db.execute(
            select(DocParseCase)
            .where(DocParseCase.dataset_id == dataset_id)
            .order_by(DocParseCase.created_at)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 执行评测
    # ------------------------------------------------------------------

    async def run_dataset(self, dataset_id: uuid.UUID) -> dict:
        """执行整个数据集的解析评测。

        对每条用例：
            - 直接提供模式：parsed_text 已提供，直接计算指标
            - Docling 端到端模式：下载 document_id 对应文件 → Docling 解析 → 转纯文本 → 计算指标
        """
        dataset = await self.get_dataset(dataset_id)
        if dataset is None:
            raise ValueError(f"数据集 {dataset_id} 不存在")

        dataset.status = "running"
        await self.db.flush()

        start_time = time.time()
        executed = 0
        metric_sums: dict[str, float] = {}

        cases = await self.list_cases(dataset_id)
        log.info(
            "doc_parse_dataset_started",
            dataset_id=str(dataset_id),
            total_cases=len(cases),
        )

        for case in cases:
            try:
                metrics = await self._execute_case(dataset_id, case)
                executed += 1
                # 累加各维度得分
                for key in ("text_similarity", "token_similarity"):
                    if key in metrics:
                        metric_sums[key] = metric_sums.get(key, 0.0) + float(metrics[key])
                metric_sums["cer"] = metric_sums.get("cer", 0.0) + float(metrics.get("cer", 0.0))
                metric_sums["table_score"] = metric_sums.get("table_score", 0.0) + float(
                    metrics.get("table", {}).get("overall_score", 0.0)
                )
                metric_sums["formula_score"] = metric_sums.get("formula_score", 0.0) + float(
                    metrics.get("formula", {}).get("overall_score", 0.0)
                )
                metric_sums["layout_score"] = metric_sums.get("layout_score", 0.0) + float(
                    metrics.get("layout", {}).get("overall_score", 0.0)
                )
                metric_sums["overall_score"] = metric_sums.get("overall_score", 0.0) + float(
                    metrics.get("overall_score", 0.0)
                )
            except Exception as exc:
                log.error("doc_parse_case_error", case_id=str(case.id), error=str(exc))
                self.db.add(DocParseResult(
                    case_id=case.id,
                    dataset_id=dataset_id,
                    parsed_text=None,
                    metrics={},
                    overall_score=0,
                    error_message=str(exc),
                    executed_at=datetime.now(timezone.utc),
                ))

        # 聚合指标（均值，CER 越低越好）
        agg_metrics: dict[str, float] = {}
        if executed > 0:
            for key, total in metric_sums.items():
                agg_metrics[key] = round(total / executed, 4)

        elapsed = int(time.time() - start_time)
        dataset.status = "completed"
        dataset.metrics = agg_metrics
        dataset.duration_seconds = elapsed

        log.info(
            "doc_parse_dataset_completed",
            dataset_id=str(dataset_id),
            executed=executed,
            avg_overall=agg_metrics.get("overall_score"),
            elapsed=elapsed,
        )
        return {
            "total": len(cases),
            "executed": executed,
            "avg_text_similarity": agg_metrics.get("text_similarity", 0.0),
            "avg_cer": agg_metrics.get("cer", 0.0),
            "avg_table_score": agg_metrics.get("table_score", 0.0),
            "avg_formula_score": agg_metrics.get("formula_score", 0.0),
            "avg_layout_score": agg_metrics.get("layout_score", 0.0),
            "avg_overall_score": agg_metrics.get("overall_score", 0.0),
            "duration_seconds": elapsed,
            "metrics": agg_metrics,
        }

    async def _execute_case(
        self,
        dataset_id: uuid.UUID,
        case: DocParseCase,
    ) -> dict:
        """执行单条用例解析并计算指标，保存结果。返回指标 dict。"""
        start = time.time()
        used_docling = False

        # 获取待评测的解析结果文本
        if case.parsed_text:
            parsed_text = case.parsed_text
        elif case.document_id:
            # Docling 端到端模式：下载文档 → Docling 解析
            parsed_text = await self._parse_with_docling(case.document_id, case.doc_type)
            used_docling = True
        else:
            # 无解析结果可评测
            raise ValueError("用例既无 parsed_text 也无 document_id，无法评测")

        parse_time_ms = int((time.time() - start) * 1000)

        # 计算指标
        metrics = compute_parse_metrics(
            standard_text=case.expected_text,
            parsed_text=parsed_text,
        )

        overall_pct = int(round(metrics["overall_score"] * 100))

        # 保存/覆盖结果
        existing = await self.db.scalar(
            select(DocParseResult).where(DocParseResult.case_id == case.id)
        )
        if existing:
            result = existing
            result.parsed_text = parsed_text
            result.metrics = metrics
            result.overall_score = overall_pct
            result.parse_time_ms = parse_time_ms
            result.used_docling = used_docling
            result.error_message = None
            result.executed_at = datetime.now(timezone.utc)
        else:
            result = DocParseResult(
                case_id=case.id,
                dataset_id=dataset_id,
                parsed_text=parsed_text,
                metrics=metrics,
                overall_score=overall_pct,
                parse_time_ms=parse_time_ms,
                used_docling=used_docling,
                executed_at=datetime.now(timezone.utc),
            )
            self.db.add(result)
        await self.db.flush()

        log.info(
            "doc_parse_case_executed",
            case_id=str(case.id),
            used_docling=used_docling,
            overall=overall_pct,
            parse_time_ms=parse_time_ms,
        )
        return metrics

    async def _parse_with_docling(
        self,
        document_id: uuid.UUID,
        doc_type: str,
    ) -> str:
        """Docling 端到端解析：下载文档 → 写临时文件 → Docling 解析 → 转纯文本。"""
        from app.models.knowledge import Document
        from app.utils.minio_client import download_file
        from app.document.docling_parser import DoclingParser

        # 查文档获取 file_path（minio://bucket/object）
        doc = await self.db.get(Document, document_id)
        if doc is None:
            raise ValueError(f"文档 {document_id} 不存在")
        if not doc.file_path:
            raise ValueError(f"文档 {document_id} 无 file_path，无法下载解析")

        # 解析 minio://bucket/object
        file_path = doc.file_path
        if file_path.startswith("minio://"):
            parts = file_path[len("minio://"):].split("/", 1)
            if len(parts) != 2:
                raise ValueError(f"file_path 格式非法: {file_path}")
            bucket, object_name = parts
        else:
            raise ValueError(f"不支持的 file_path 格式: {file_path}")

        # 下载文件
        file_bytes = await download_file(bucket, object_name)

        # 写临时文件
        suffix = _DOC_SUFFIX_MAP.get(doc_type.lower(), ".bin")
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(file_bytes)
            # Docling 解析
            parser = DoclingParser()
            html = await parser.parse(tmp_path)
            if not html:
                raise ValueError("Docling 解析返回空结果（可能 Docling 不可用或文档无法解析）")
            return _strip_html_to_text(html)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 查询结果
    # ------------------------------------------------------------------

    async def get_dataset_results(self, dataset_id: uuid.UUID) -> list[dict]:
        """获取数据集下所有用例的解析结果与指标。"""
        result = await self.db.execute(
            select(DocParseCase, DocParseResult)
            .outerjoin(
                DocParseResult,
                DocParseResult.case_id == DocParseCase.id,
            )
            .where(DocParseCase.dataset_id == dataset_id)
            .order_by(DocParseCase.created_at)
        )
        rows = result.all()
        results: list[dict] = []
        for case, res in rows:
            results.append({
                "case_id": str(case.id),
                "title": case.title,
                "doc_type": case.doc_type,
                "difficulty": case.difficulty,
                "expected_text": case.expected_text,
                "parsed_text": res.parsed_text if res else case.parsed_text,
                "metrics": res.metrics if res else None,
                "overall_score": res.overall_score if res else 0,
                "parse_time_ms": res.parse_time_ms if res else 0,
                "used_docling": res.used_docling if res else False,
                "error_message": res.error_message if res else None,
                "executed_at": res.executed_at.isoformat() if res and res.executed_at else None,
            })
        return results

    # ------------------------------------------------------------------
    # 全局统计
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        """获取文档解析评测全局统计。"""
        total_datasets = await self.db.scalar(
            select(func.count())
            .select_from(DocParseDataset)
            .where(DocParseDataset.deleted_at.is_(None))
        ) or 0

        total_cases = await self.db.scalar(
            select(func.count())
            .select_from(DocParseCase)
            .join(DocParseDataset, DocParseDataset.id == DocParseCase.dataset_id)
            .where(DocParseDataset.deleted_at.is_(None))
        ) or 0

        total_executed = await self.db.scalar(
            select(func.count()).select_from(DocParseResult)
        ) or 0

        # 聚合已执行结果
        ds_result = await self.db.execute(
            select(DocParseDataset).where(
                DocParseDataset.deleted_at.is_(None),
                DocParseDataset.status == "completed",
                DocParseDataset.metrics.isnot(None),
            )
        )
        completed = list(ds_result.scalars().all())
        agg_keys = [
            "text_similarity", "cer", "table_score",
            "formula_score", "layout_score", "overall_score",
        ]
        sums = {k: 0.0 for k in agg_keys}
        weight = 0
        for ds in completed:
            m = ds.metrics or {}
            w = ds.total_cases or 0
            weight += w
            for k in agg_keys:
                if k in m:
                    sums[k] += float(m[k]) * w

        return {
            "total_datasets": total_datasets,
            "total_cases": total_cases,
            "total_executed": total_executed,
            "avg_text_similarity": round(sums["text_similarity"] / weight, 4) if weight else 0.0,
            "avg_cer": round(sums["cer"] / weight, 4) if weight else 0.0,
            "avg_table_score": round(sums["table_score"] / weight, 4) if weight else 0.0,
            "avg_formula_score": round(sums["formula_score"] / weight, 4) if weight else 0.0,
            "avg_layout_score": round(sums["layout_score"] / weight, 4) if weight else 0.0,
            "avg_overall_score": round(sums["overall_score"] / weight, 4) if weight else 0.0,
        }
