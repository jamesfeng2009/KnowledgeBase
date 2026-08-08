"""
微调数据集构建任务 — 单一职责：异步构建数据集并回写导出记录。

流程：加载 DatasetExport → status=building → 调 DATASET_BUILDERS 对应构建函数
→ JSONL 导出（app.finetune.exporter）→ 回写 sample_count/filtered_stats/
file_path/file_size/status=completed/completed_at。

幂等性：同 version 导出为覆盖写（见 exporter），记录字段整体回写，
重复执行结果一致，可安全重试。
异常处理：status=failed 后 raise self.retry（与 recommendation_tasks 约定一致——
返回 failed dict 会让 Celery 判定任务成功，autoretry 失效）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from celery_app import celery_app
from app.utils.logger import get_logger
from app.utils.retry import make_celery_retry_kwargs

logger = get_logger(__name__)


@celery_app.task(
    name="tasks.finetune_tasks.build_dataset_task",
    bind=True,
    **make_celery_retry_kwargs(),
)
def build_dataset_task(self, export_id: str) -> dict[str, Any]:
    """构建微调数据集 — 按 DatasetExport 记录的类型与参数执行构建。

    Args:
        export_id: DatasetExport 记录 ID（字符串）。

    Returns:
        构建结果统计（export_id / dataset_type / sample_count / file_size_bytes）。
    """
    logger.info("finetune.build_started", export_id=export_id)
    try:
        result = asyncio.run(_build_async(export_id, celery_task_id=self.request.id))
        logger.info(
            "finetune.build_completed",
            export_id=export_id,
            sample_count=result.get("sample_count", 0),
        )
        return result
    except Exception as exc:
        logger.error("finetune.build_failed", export_id=export_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------
# 异步实现
# ------------------------------------------------------------------


async def _build_async(
    export_id: str,
    *,
    celery_task_id: str | None = None,
) -> dict[str, Any]:
    """异步构建实现 — 状态流转 + 构建 + 导出 + 回写。"""
    from sqlalchemy import select

    from app.database import task_db_session
    from app.finetune.dataset_builder import DATASET_BUILDERS
    from app.finetune.exporter import export_jsonl, make_version
    from app.models.finetune import DatasetExport

    async with task_db_session() as session:
        # 1. 加载导出记录
        record = (
            await session.execute(
                select(DatasetExport).where(DatasetExport.id == uuid.UUID(export_id))
            )
        ).scalar_one_or_none()
        if record is None:
            # 记录不存在无需重试（永久性错误），直接抛出终止任务
            raise ValueError(f"DatasetExport 不存在: {export_id}")

        builder = DATASET_BUILDERS.get(record.dataset_type)
        if builder is None:
            raise ValueError(f"未知数据集类型: {record.dataset_type}")

        # 2. status → building
        record.status = "building"
        if celery_task_id:
            record.celery_task_id = celery_task_id
        await session.commit()

        try:
            # 3. 调构建函数（params 落库于创建时，此处透传）
            params = dict(record.params or {})
            samples, stats = await builder(session, record.tenant_id, **params)

            # 4. 导出 JSONL（version 创建时已生成，目录与之一致）
            version = record.version or make_version()
            if record.version != version:
                record.version = version
            file_path, file_size = export_jsonl(
                samples,
                str(record.tenant_id) if record.tenant_id else None,
                record.dataset_type,
                version,
            )

            # 5. 回写完成态
            record.sample_count = len(samples)
            record.filtered_stats = stats
            record.file_path = str(file_path)
            record.file_size_bytes = file_size
            record.status = "completed"
            record.completed_at = datetime.now(timezone.utc)
            await session.commit()
        except Exception as exc:
            # 回写失败态后重抛，由外层 self.retry 接管
            record.status = "failed"
            await session.commit()
            raise exc

        return {
            "status": "success",
            "export_id": export_id,
            "dataset_type": record.dataset_type,
            "version": record.version,
            "sample_count": record.sample_count,
            "file_size_bytes": record.file_size_bytes,
        }
