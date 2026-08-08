"""
微调数据集路由 — 单一职责：微调数据集导出任务的 HTTP 端点。

端点（全部 admin 限定）：
    POST /finetune/datasets/export              — 创建导出任务（Celery 异步构建）
    GET  /finetune/datasets                     — 分页查询导出记录
    GET  /finetune/datasets/{export_id}         — 单条详情
    GET  /finetune/datasets/{export_id}/preview — 预览 JSONL 前 N 行
    GET  /finetune/datasets/{export_id}/download— 下载 JSONL 文件

权限：所有端点要求 admin/kb_admin 角色（与 recommendations.rebuild 同口径）；
多租户隔离：记录按 request.state.tenant_id 归属，查询/预览/下载均限定本租户。
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.finetune.exporter import make_version, read_jsonl_head
from app.models.finetune import DatasetExport
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.utils.logger import get_logger
from app.utils.tenant import apply_tenant_filter

logger = get_logger(__name__)

router = APIRouter(prefix="/finetune", tags=["微调数据集"])

#: 允许的数据集类型（与 dataset_builder.DATASET_BUILDERS 键一致）
DatasetType = Literal["sft", "dpo", "embedding", "golden"]

#: 允许的密级阈值（与 dataset_builder.CLASSIFICATION_WEIGHT 键一致）
ClassificationLevel = Literal["public", "internal", "confidential", "secret"]


class DatasetExportRequest(BaseModel):
    """创建数据集导出任务的请求体。"""

    dataset_type: DatasetType = Field(..., description="数据集类型")
    max_classification: ClassificationLevel = Field(
        default="internal", description="密级阈值（超过即剔除）"
    )
    days: int = Field(default=90, ge=1, le=365, description="数据时间窗口（天）")
    min_rating: int = Field(default=4, ge=1, le=5, description="好评最低评分")
    limit: int = Field(default=10000, ge=1, le=100000, description="样本上限")


def _require_admin(user: User) -> ApiResponse[None] | None:
    """管理员校验 — 非 admin/kb_admin 返回 403 响应，否则 None。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")
    return None


def _serialize(record: DatasetExport) -> dict[str, Any]:
    """序列化导出记录（datetime → ISO 字符串）。"""
    return {
        "id": str(record.id),
        "dataset_type": record.dataset_type,
        "version": record.version,
        "status": record.status,
        "sample_count": record.sample_count,
        "filtered_stats": record.filtered_stats or {},
        "file_size_bytes": record.file_size_bytes,
        "celery_task_id": record.celery_task_id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "completed_at": (
            record.completed_at.isoformat() if record.completed_at else None
        ),
    }


async def _load_record(
    db: AsyncSession,
    export_id: UUID,
    tenant_id: UUID | None,
) -> DatasetExport | None:
    """加载导出记录 — 含租户隔离（非本租户记录视为不存在）。"""
    stmt = select(DatasetExport).where(DatasetExport.id == export_id)
    stmt = apply_tenant_filter(stmt, DatasetExport, tenant_id)
    return (await db.execute(stmt)).scalar_one_or_none()


@router.post("/datasets/export", response_model=ApiResponse[dict])
async def export_dataset(
    request: Request,
    body: DatasetExportRequest = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """创建数据集导出任务 — 落库 pending 记录并提交 Celery 异步构建。"""
    if (deny := _require_admin(user)) is not None:
        return deny

    tenant_id = getattr(request.state, "tenant_id", None)
    params = {
        "max_classification": body.max_classification,
        "days": body.days,
        "min_rating": body.min_rating,
        "limit": body.limit,
    }
    record = DatasetExport(
        tenant_id=tenant_id,
        dataset_type=body.dataset_type,
        version=make_version(),
        status="pending",
        params=params,
        created_by=user.id,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    try:
        from tasks.finetune_tasks import build_dataset_task

        async_result = build_dataset_task.delay(export_id=str(record.id))
        # 回写 task_id（便于事后追踪；失败不阻塞主流程）
        record.celery_task_id = async_result.id
        await db.commit()
    except Exception as exc:
        # Celery broker 不可用时不应 500，记录保持 pending 供排查后重提
        logger.error(
            "finetune.export.submit_failed",
            export_id=str(record.id),
            error=str(exc),
        )
        return ApiResponse(code=500, data=None, message=f"构建任务提交失败: {exc}")

    return ApiResponse(
        code=0,
        data={
            "export_id": str(record.id),
            "task_id": async_result.id,
            "status": "building",
        },
        message="数据集构建任务已提交",
    )


@router.get("/datasets", response_model=ApiResponse[PageResponse[dict]])
async def list_datasets(
    request: Request,
    dataset_type: DatasetType | None = Query(default=None, description="类型过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[dict]]:
    """分页查询数据集导出记录（本租户），可按类型过滤。"""
    if (deny := _require_admin(user)) is not None:
        return deny

    tenant_id = getattr(request.state, "tenant_id", None)
    stmt = select(DatasetExport)
    count_stmt = select(func.count(DatasetExport.id))
    if dataset_type:
        stmt = stmt.where(DatasetExport.dataset_type == dataset_type)
        count_stmt = count_stmt.where(DatasetExport.dataset_type == dataset_type)
    stmt = apply_tenant_filter(stmt, DatasetExport, tenant_id)
    count_stmt = apply_tenant_filter(count_stmt, DatasetExport, tenant_id)

    total = (await db.execute(count_stmt)).scalar_one()
    records = (
        await db.execute(
            stmt.order_by(DatasetExport.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()

    return ApiResponse(
        code=0,
        data=PageResponse[dict](
            items=[_serialize(r) for r in records],
            total=total,
            page=page,
            size=size,
            pages=math.ceil(total / size) if total else 0,
        ),
        message="success",
    )


@router.get("/datasets/{export_id}", response_model=ApiResponse[dict])
async def get_dataset(
    request: Request,
    export_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """查询单条导出记录详情。"""
    if (deny := _require_admin(user)) is not None:
        return deny

    tenant_id = getattr(request.state, "tenant_id", None)
    record = await _load_record(db, export_id, tenant_id)
    if record is None:
        return ApiResponse(code=404, data=None, message="导出记录不存在")
    data = _serialize(record)
    data["file_path"] = record.file_path
    data["params"] = record.params or {}
    return ApiResponse(code=0, data=data, message="success")


def _resolve_export_file(record: DatasetExport) -> Path | None:
    """解析导出文件路径 — 记录未完成或文件缺失时返回 None。"""
    if record.status != "completed" or not record.file_path:
        return None
    path = Path(record.file_path)
    return path if path.is_file() else None


@router.get("/datasets/{export_id}/preview", response_model=ApiResponse[dict])
async def preview_dataset(
    request: Request,
    export_id: UUID,
    limit: int = Query(default=5, ge=1, le=100, description="预览行数"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """预览数据集 — 读取 JSONL 前 N 行。"""
    if (deny := _require_admin(user)) is not None:
        return deny

    tenant_id = getattr(request.state, "tenant_id", None)
    record = await _load_record(db, export_id, tenant_id)
    if record is None:
        return ApiResponse(code=404, data=None, message="导出记录不存在")
    path = _resolve_export_file(record)
    if path is None:
        return ApiResponse(code=400, data=None, message="数据集尚未构建完成或文件缺失")
    return ApiResponse(
        code=0, data={"items": read_jsonl_head(path, limit)}, message="success"
    )


@router.get("/datasets/{export_id}/download")
async def download_dataset(
    request: Request,
    export_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> Any:
    """下载数据集 JSONL 文件（application/x-ndjson）。"""
    if (deny := _require_admin(user)) is not None:
        return deny

    tenant_id = getattr(request.state, "tenant_id", None)
    record = await _load_record(db, export_id, tenant_id)
    if record is None:
        return ApiResponse(code=404, data=None, message="导出记录不存在")
    path = _resolve_export_file(record)
    if path is None:
        return ApiResponse(code=400, data=None, message="数据集尚未构建完成或文件缺失")
    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=f"{record.dataset_type}-{record.version}.jsonl",
    )
