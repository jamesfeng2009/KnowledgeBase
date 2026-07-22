"""
知识回流层 API — 单一职责：提供知识回流的 HTTP 端点。

端点分组：
    知识提取     /compounding/extract
    知识资产     /compounding/assets
    冲突检测     /compounding/conflicts
    复用注入     /compounding/reuse
    回流任务     /compounding/tasks
    回流统计     /compounding/stats

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑委托给 KnowledgeCompoundingService。

LLM 依赖端点（知识提取 / 冲突检测 / 复用注入）通过 ``get_llm_provider``
获取 Provider，不可用时返回 503；纯查询端点不依赖 LLM。

所有端点通过 ``require_module("testing_platform")`` 进行租户模块门控
（知识回流属于测试平台模块的子能力）。
"""

from __future__ import annotations

import math
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.llm.factory import get_llm_provider
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.knowledge_compounding import (
    CompoundingStatsResponse,
    CompoundingTaskResponse,
    ConflictDetectionRequest,
    ConflictResolveRequest,
    ExtractionRequest,
    KnowledgeAssetResponse,
    KnowledgeConflictResponse,
    ReuseInjectionRequest,
    ReuseInjectionResult,
)
from app.services.knowledge_compounding import KnowledgeCompoundingService
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/compounding", tags=["知识回流层"])


# ======================================================================
# 内部工具
# ======================================================================


def _paginated(items: list, total: int, page: int, size: int) -> PageResponse:
    """从 ``(items, total)`` 元组构建 ``PageResponse``。"""
    return PageResponse(
        items=items,
        total=total,
        page=page,
        size=size,
        pages=math.ceil(total / size) if size else 0,
    )


def _get_llm_or_none():
    """获取 LLM Provider，不可用时返回 None。"""
    try:
        return get_llm_provider()
    except Exception:
        return None


# ======================================================================
# 知识提取
# ======================================================================


@router.post("/extract")
async def extract_knowledge(
    request: Request,
    body: ExtractionRequest,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """从测试执行结果提取知识资产 — 串联 Step 1~4 的完整流程。

    收集执行结果 → AI 知识提取 → 4 类资产沉淀 → 冲突检测。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    try:
        llm = get_llm_provider()
    except Exception:
        return ApiResponse(code=503, data=None, message="LLM 服务不可用")

    service = KnowledgeCompoundingService(llm, db, tenant_id=tenant_id)
    try:
        result = await service.extract_knowledge(
            body.execution_id,
            trigger_source=body.trigger_source,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=result, message="success")


# ======================================================================
# 知识资产
# ======================================================================


@router.get("/assets")
async def list_assets(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None, description="项目 ID"),
    asset_type: str | None = Query(default=None, description="资产类型"),
    status: str | None = Query(default=None, description="资产状态"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询知识资产列表 — 支持按项目/类型/状态筛选。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeCompoundingService(_get_llm_or_none(), db, tenant_id=tenant_id)
    assets, total = await service.list_assets(
        project_id=project_id,
        asset_type=asset_type,
        status=status,
        page=page,
        size=size,
    )
    return ApiResponse(
        code=0,
        data=_paginated(
            [KnowledgeAssetResponse.model_validate(a) for a in assets],
            total,
            page,
            size,
        ),
        message="success",
    )


@router.get("/assets/{asset_id}")
async def get_asset(
    request: Request,
    asset_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取知识资产详情。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeCompoundingService(_get_llm_or_none(), db, tenant_id=tenant_id)
    asset = await service.get_asset(asset_id)
    if asset is None:
        return ApiResponse(code=404, data=None, message="知识资产不存在")
    return ApiResponse(
        code=0,
        data=KnowledgeAssetResponse.model_validate(asset),
        message="success",
    )


# ======================================================================
# 冲突检测
# ======================================================================


@router.post("/conflicts/detect")
async def detect_conflicts(
    request: Request,
    body: ConflictDetectionRequest,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """检测指定知识资产与已有资产的冲突。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    try:
        llm = get_llm_provider()
    except Exception:
        return ApiResponse(code=503, data=None, message="LLM 服务不可用")

    service = KnowledgeCompoundingService(llm, db, tenant_id=tenant_id)
    try:
        conflicts = await service.detect_conflicts(body.asset_id)
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data={"conflicts": conflicts, "count": len(conflicts)},
        message="success",
    )


@router.get("/conflicts")
async def list_conflicts(
    request: Request,
    resolution: str | None = Query(default=None, description="解决方案"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询知识冲突列表。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeCompoundingService(_get_llm_or_none(), db, tenant_id=tenant_id)
    conflicts, total = await service.list_conflicts(
        resolution=resolution,
        page=page,
        size=size,
    )
    return ApiResponse(
        code=0,
        data=_paginated(
            [KnowledgeConflictResponse.model_validate(c) for c in conflicts],
            total,
            page,
            size,
        ),
        message="success",
    )


@router.put("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    request: Request,
    conflict_id: uuid.UUID,
    body: ConflictResolveRequest,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """解决知识冲突 — 更新解决方案和资产状态。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeCompoundingService(_get_llm_or_none(), db, tenant_id=tenant_id)
    try:
        conflict = await service.resolve_conflict(
            conflict_id,
            resolution=body.resolution,
            note=body.note,
            resolved_by=user.id,
        )
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=KnowledgeConflictResponse.model_validate(conflict),
        message="success",
    )


# ======================================================================
# 复用注入
# ======================================================================


@router.post("/reuse/inject")
async def inject_for_reuse(
    request: Request,
    body: ReuseInjectionRequest,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """复用注入 — 检索历史知识资产注入用例生成上下文。

    实现知识复利：历史测试经验自动回流到下一轮用例生成。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeCompoundingService(_get_llm_or_none(), db, tenant_id=tenant_id)
    try:
        result = await service.inject_for_reuse(
            body.requirement_id,
            max_assets=body.max_assets,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=ReuseInjectionResult(
            requirement_id=result.get("requirement_id", body.requirement_id),
            injected_assets=result.get("injected_assets", []),
            injection_context=result.get("injection_context"),
            asset_count=result.get("asset_count", 0),
        ),
        message="success",
    )


# ======================================================================
# 回流任务
# ======================================================================


@router.get("/tasks")
async def list_tasks(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None, description="项目 ID"),
    task_type: str | None = Query(default=None, description="任务类型"),
    status: str | None = Query(default=None, description="任务状态"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询回流任务列表。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeCompoundingService(_get_llm_or_none(), db, tenant_id=tenant_id)
    tasks, total = await service.list_tasks(
        project_id=project_id,
        task_type=task_type,
        status=status,
        page=page,
        size=size,
    )
    return ApiResponse(
        code=0,
        data=_paginated(
            [CompoundingTaskResponse.model_validate(t) for t in tasks],
            total,
            page,
            size,
        ),
        message="success",
    )


# ======================================================================
# 回流统计
# ======================================================================


@router.get("/stats")
async def get_stats(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None, description="项目 ID（可选）"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取知识回流统计数据 — 资产 / 任务 / 冲突 / 复用注入的多维度聚合。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeCompoundingService(_get_llm_or_none(), db, tenant_id=tenant_id)
    stats = await service.get_stats(project_id=project_id)
    return ApiResponse(
        code=0,
        data=CompoundingStatsResponse.model_validate(stats),
        message="success",
    )
