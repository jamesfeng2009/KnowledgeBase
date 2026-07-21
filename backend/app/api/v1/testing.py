"""
智能测试平台 API — 单一职责：提供测试平台的 HTTP 端点。

端点分组：
    项目管理     /testing/projects
    需求点       /testing/requirements
    测试用例     /testing/cases
    用例评审     /testing/reviews
    测试计划     /testing/plans
    执行记录     /testing/executions
    平台统计     /testing/stats

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑委托给 ``app.services.testing`` 下的各 Service。

LLM 依赖端点（需求提取 / 用例生成 / 计划编排）通过 ``get_llm_provider``
获取 Provider，不可用时返回 503；纯 CRUD 端点不依赖 LLM。

所有端点通过 ``require_module("testing_platform")`` 进行租户模块门控。
"""

from __future__ import annotations

import math
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.llm.factory import get_llm_provider
from app.models.testing import TestProject
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.testing import (
    RequirementExtractRequest,
    TestCaseCreate,
    TestCaseGenerateRequest,
    TestCaseResponse,
    TestCaseUpdate,
    TestExecutionCreate,
    TestExecutionResponse,
    TestPlanCreate,
    TestPlanOrchestrateRequest,
    TestPlanResponse,
    TestProjectCreate,
    TestProjectResponse,
    TestProjectUpdate,
    TestRequirementResponse,
    TestRequirementUpdate,
    TestReviewAction,
    TestReviewResponse,
    TestReviewSubmit,
    TestingStatsResponse,
)
from app.services.testing import (
    RequirementAnalysisService,
    TestCaseGenerationService,
    TestCaseManagementService,
    TestOrchestrationService,
    TestReviewService,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/testing", tags=["智能测试平台"])


# ======================================================================
# 内部工具
# ======================================================================


class BatchStatusUpdateBody(BaseModel):
    """批量更新用例状态请求体。"""

    case_ids: list[uuid.UUID] = Field(..., description="用例 ID 列表")
    status: str = Field(..., description="目标状态")


def _paginated(items: list, total: int, page: int, size: int) -> PageResponse:
    """从 ``(items, total)`` 元组构建 ``PageResponse`` — 自动计算总页数。

    测试平台 Service 的分页方法返回 ``(list, int)`` 元组而非 ``PageResult``
    对象，本辅助函数补全 page / size / pages 字段。
    """
    return PageResponse(
        items=items,
        total=total,
        page=page,
        size=size,
        pages=math.ceil(total / size) if size else 0,
    )


def _get_llm_or_none():
    """获取 LLM Provider，不可用时返回 None。

    用于不依赖 LLM 的 Service 方法（如 list / get / update），
    避免因 LLM 不可用而阻塞纯 CRUD 操作。
    """
    try:
        return get_llm_provider()
    except Exception:
        return None


# ======================================================================
# 项目管理
# ======================================================================


@router.post("/projects", status_code=201)
async def create_project(
    body: TestProjectCreate,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """创建测试项目 — 关联 PRD / 技术方案 / 接口文档。"""
    project = TestProject(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        prd_doc_ids=body.prd_doc_ids,
        tech_doc_ids=body.tech_doc_ids,
        api_doc_ids=body.api_doc_ids,
    )
    db.add(project)
    await db.flush()
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestProjectResponse.model_validate(project),
        message="success",
    )


@router.get("/projects")
async def list_projects(
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询测试项目列表。"""
    conditions = [TestProject.deleted_at.is_(None)]

    total = await db.scalar(
        select(func.count()).select_from(TestProject).where(*conditions)
    ) or 0

    offset = (page - 1) * size
    result = await db.execute(
        select(TestProject)
        .where(*conditions)
        .order_by(TestProject.created_at.desc())
        .offset(offset)
        .limit(size)
    )
    projects = result.scalars().all()

    return ApiResponse(
        code=0,
        data=_paginated(
            [TestProjectResponse.model_validate(p) for p in projects],
            total,
            page,
            size,
        ),
        message="success",
    )


@router.get("/projects/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取测试项目详情。"""
    result = await db.execute(
        select(TestProject).where(
            TestProject.id == project_id,
            TestProject.deleted_at.is_(None),
        )
    )
    project = result.scalars().first()
    if not project:
        return ApiResponse(code=404, data=None, message="项目不存在")
    return ApiResponse(
        code=0,
        data=TestProjectResponse.model_validate(project),
        message="success",
    )


@router.put("/projects/{project_id}")
async def update_project(
    project_id: uuid.UUID,
    body: TestProjectUpdate,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """更新测试项目 — 仅更新提供的字段。"""
    result = await db.execute(
        select(TestProject).where(
            TestProject.id == project_id,
            TestProject.deleted_at.is_(None),
        )
    )
    project = result.scalars().first()
    if not project:
        return ApiResponse(code=404, data=None, message="项目不存在")

    update_data = body.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in update_data.items():
        if hasattr(project, key):
            setattr(project, key, value)
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestProjectResponse.model_validate(project),
        message="success",
    )


# ======================================================================
# 需求点
# ======================================================================


@router.post("/requirements/extract")
async def extract_requirements(
    body: RequirementExtractRequest,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """从 PRD / UI 稿文档自动提取原子需求点 — 调用 LLM 分析。"""
    try:
        llm = get_llm_provider()
    except Exception:
        return ApiResponse(code=503, data=None, message="LLM 服务不可用")

    service = RequirementAnalysisService(llm, db)
    try:
        result = await service.extract_requirements(
            body.project_id,
            body.doc_id,
            [c.value for c in body.target_categories]
            if body.target_categories
            else None,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=result, message="success")


@router.get("/requirements")
async def list_requirements(
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询项目的需求点列表。"""
    service = RequirementAnalysisService(_get_llm_or_none(), db)
    items, total = await service.list_requirements(project_id, page=page, size=size)
    return ApiResponse(
        code=0,
        data=_paginated(items, total, page, size),
        message="success",
    )


@router.get("/requirements/{requirement_id}")
async def get_requirement(
    requirement_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取需求点详情。"""
    service = RequirementAnalysisService(_get_llm_or_none(), db)
    requirement = await service.get_requirement(requirement_id)
    if requirement is None:
        return ApiResponse(code=404, data=None, message="需求点不存在")
    return ApiResponse(
        code=0,
        data=TestRequirementResponse.model_validate(requirement),
        message="success",
    )


@router.put("/requirements/{requirement_id}")
async def update_requirement(
    requirement_id: uuid.UUID,
    body: TestRequirementUpdate,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """更新需求点 — 仅更新提供的字段。"""
    service = RequirementAnalysisService(_get_llm_or_none(), db)
    update_data = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        requirement = await service.update_requirement(
            requirement_id, **update_data
        )
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestRequirementResponse.model_validate(requirement),
        message="success",
    )


# ======================================================================
# 测试用例
# ======================================================================


@router.post("/cases/generate")
async def generate_cases(
    body: TestCaseGenerateRequest,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """AI 生成测试用例 — 基于需求点 + 上下文文档调用 LLM 生成。"""
    try:
        llm = get_llm_provider()
    except Exception:
        return ApiResponse(code=503, data=None, message="LLM 服务不可用")

    service = TestCaseGenerationService(llm, db)
    try:
        result = await service.generate_cases(
            body.requirement_id,
            context_doc_ids=body.context_doc_ids,
            test_type=body.test_type.value if body.test_type else None,
            max_cases=body.max_cases,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=result, message="success")


@router.post("/cases", status_code=201)
async def create_case(
    body: TestCaseCreate,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """手动创建测试用例。"""
    service = TestCaseManagementService(db)
    try:
        case = await service.create_case(
            project_id=uuid.UUID(body.project_id),
            title=body.title,
            description=body.description,
            preconditions=body.preconditions,
            test_steps=[s.model_dump() for s in body.test_steps]
            if body.test_steps
            else None,
            expected_result=body.expected_result,
            test_type=body.test_type.value,
            priority=body.priority.value,
            tags=body.tags,
            requirement_id=uuid.UUID(body.requirement_id)
            if body.requirement_id
            else None,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestCaseResponse.model_validate(case),
        message="success",
    )


@router.get("/cases")
async def list_cases(
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    status: str | None = Query(default=None, description="用例状态"),
    test_type: str | None = Query(default=None, description="测试类型"),
    priority: str | None = Query(default=None, description="优先级"),
    keyword: str | None = Query(default=None, description="标题关键字"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询测试用例 — 支持多维度筛选与标题关键字搜索。"""
    service = TestCaseManagementService(db)
    cases, total = await service.list_cases(
        project_id=project_id,
        status=status,
        test_type=test_type,
        priority=priority,
        keyword=keyword,
        page=page,
        size=size,
    )
    return ApiResponse(
        code=0,
        data=_paginated(
            [TestCaseResponse.model_validate(c) for c in cases],
            total,
            page,
            size,
        ),
        message="success",
    )


@router.get("/cases/{case_id}")
async def get_case(
    case_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取测试用例详情。"""
    service = TestCaseManagementService(db)
    case = await service.get_case(case_id)
    if case is None:
        return ApiResponse(code=404, data=None, message="测试用例不存在")
    return ApiResponse(
        code=0,
        data=TestCaseResponse.model_validate(case),
        message="success",
    )


@router.put("/cases/{case_id}")
async def update_case(
    case_id: uuid.UUID,
    body: TestCaseUpdate,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """更新测试用例 — 仅更新提供的字段。"""
    service = TestCaseManagementService(db)
    update_data = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        case = await service.update_case(case_id, **update_data)
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestCaseResponse.model_validate(case),
        message="success",
    )


@router.delete("/cases/{case_id}")
async def delete_case(
    case_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """软删除测试用例 — 设置 deleted_at，不物理删除。"""
    service = TestCaseManagementService(db)
    try:
        await service.delete_case(case_id)
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(code=0, data=None, message="success")


@router.post("/cases/batch-status")
async def batch_update_status(
    body: BatchStatusUpdateBody,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """批量更新用例状态 — 仅更新未软删除的用例。"""
    service = TestCaseManagementService(db)
    updated_count = await service.batch_update_status(body.case_ids, body.status)
    await db.commit()
    return ApiResponse(
        code=0,
        data={"updated_count": updated_count},
        message="success",
    )


# ======================================================================
# 用例评审
# ======================================================================


@router.post("/reviews", status_code=201)
async def submit_for_review(
    body: TestReviewSubmit,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """提交用例评审 — 将用例状态变更为 pending_review。"""
    service = TestReviewService(db, user)
    try:
        review = await service.submit_for_review(
            uuid.UUID(body.case_id),
            comment=body.comment,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestReviewResponse.model_validate(review),
        message="success",
    )


@router.get("/reviews/pending")
async def list_pending_reviews(
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询待评审列表。"""
    service = TestReviewService(db, user)
    reviews, total = await service.get_pending_reviews(page=page, size=size)
    return ApiResponse(
        code=0,
        data=_paginated(
            [TestReviewResponse.model_validate(r) for r in reviews],
            total,
            page,
            size,
        ),
        message="success",
    )


@router.get("/reviews/{review_id}")
async def get_review(
    review_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取评审详情。"""
    service = TestReviewService(db, user)
    review = await service.get_review(review_id)
    if review is None:
        return ApiResponse(code=404, data=None, message="评审记录不存在")
    return ApiResponse(
        code=0,
        data=TestReviewResponse.model_validate(review),
        message="success",
    )


@router.put("/reviews/{review_id}/approve")
async def approve_review(
    review_id: uuid.UUID,
    body: TestReviewAction,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """通过评审 — 评审状态变更为 approved，用例状态联动为 approved。"""
    service = TestReviewService(db, user)
    try:
        review = await service.approve(
            review_id,
            comment=body.comment,
            suggestions=body.suggestions,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestReviewResponse.model_validate(review),
        message="success",
    )


@router.put("/reviews/{review_id}/reject")
async def reject_review(
    review_id: uuid.UUID,
    body: TestReviewAction,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """驳回评审 — 评审状态变更为 rejected，用例状态回退为 draft。"""
    service = TestReviewService(db, user)
    try:
        review = await service.reject(
            review_id,
            comment=body.comment,
            suggestions=body.suggestions,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestReviewResponse.model_validate(review),
        message="success",
    )


@router.get("/cases/{case_id}/reviews")
async def get_case_reviews(
    case_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """查询某用例的全部评审历史。"""
    service = TestReviewService(db, user)
    reviews = await service.get_reviews_by_case(case_id)
    return ApiResponse(
        code=0,
        data=[TestReviewResponse.model_validate(r) for r in reviews],
        message="success",
    )


# ======================================================================
# 测试计划
# ======================================================================


@router.post("/plans", status_code=201)
async def create_plan(
    body: TestPlanCreate,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """创建测试计划 — 初始状态为 draft，不依赖 LLM。"""
    service = TestOrchestrationService(_get_llm_or_none(), db)
    plan = await service.create_plan(
        project_id=uuid.UUID(body.project_id),
        name=body.name,
        description=body.description,
        case_ids=body.case_ids or [],
        execution_strategy=body.execution_strategy.value,
        user_id=user.id,
    )
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestPlanResponse.model_validate(plan),
        message="success",
    )


@router.post("/plans/{plan_id}/orchestrate")
async def orchestrate_plan(
    plan_id: uuid.UUID,
    body: TestPlanOrchestrateRequest,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """AI 编排测试计划 — 调用 LLM 生成执行顺序与节点分配方案。"""
    try:
        llm = get_llm_provider()
    except Exception:
        return ApiResponse(code=503, data=None, message="LLM 服务不可用")

    service = TestOrchestrationService(llm, db)
    try:
        plan = await service.orchestrate(
            plan_id,
            node_count=body.node_count,
            consider_dependencies=body.consider_dependencies,
        )
    except ValueError as exc:
        return ApiResponse(code=400, data=None, message=str(exc))
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestPlanResponse.model_validate(plan),
        message="success",
    )


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: uuid.UUID,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取测试计划详情。"""
    service = TestOrchestrationService(_get_llm_or_none(), db)
    plan = await service.get_plan(plan_id)
    if plan is None:
        return ApiResponse(code=404, data=None, message="测试计划不存在")
    return ApiResponse(
        code=0,
        data=TestPlanResponse.model_validate(plan),
        message="success",
    )


@router.get("/plans")
async def list_plans(
    project_id: uuid.UUID = Query(..., description="项目 ID"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询项目的测试计划列表。"""
    service = TestOrchestrationService(_get_llm_or_none(), db)
    plans, total = await service.list_plans(project_id, page=page, size=size)
    return ApiResponse(
        code=0,
        data=_paginated(
            [TestPlanResponse.model_validate(p) for p in plans],
            total,
            page,
            size,
        ),
        message="success",
    )


# ======================================================================
# 执行记录
# ======================================================================


@router.post("/executions", status_code=201)
async def record_execution(
    body: TestExecutionCreate,
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """记录用例执行结果 — 根据状态自动设置时间戳。"""
    service = TestOrchestrationService(_get_llm_or_none(), db)
    execution = await service.record_execution(
        case_id=uuid.UUID(body.case_id),
        plan_id=uuid.UUID(body.plan_id) if body.plan_id else None,
        executor=body.executor,
        status=body.status.value,
        result=body.result,
        execution_log=body.execution_log,
        failure_reason=body.failure_reason,
        duration_seconds=body.duration_seconds,
        executor_id=user.id,
    )
    await db.commit()
    return ApiResponse(
        code=0,
        data=TestExecutionResponse.model_validate(execution),
        message="success",
    )


@router.get("/executions")
async def list_executions(
    plan_id: uuid.UUID | None = Query(default=None, description="计划 ID"),
    case_id: uuid.UUID | None = Query(default=None, description="用例 ID"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """分页查询执行记录 — 支持按计划或用例过滤。"""
    service = TestOrchestrationService(_get_llm_or_none(), db)
    executions, total = await service.list_executions(
        plan_id=plan_id,
        case_id=case_id,
        page=page,
        size=size,
    )
    return ApiResponse(
        code=0,
        data=_paginated(
            [TestExecutionResponse.model_validate(e) for e in executions],
            total,
            page,
            size,
        ),
        message="success",
    )


# ======================================================================
# 平台统计
# ======================================================================


@router.get("/stats")
async def get_stats(
    project_id: uuid.UUID | None = Query(default=None, description="项目 ID（可选）"),
    user: User = Depends(require_module("testing_platform")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取测试平台统计数据 — 用例 / 需求 / 计划 / 执行的多维度聚合。"""
    service = TestCaseManagementService(db)
    stats = await service.get_stats(project_id=project_id)
    return ApiResponse(
        code=0,
        data=TestingStatsResponse.model_validate(stats),
        message="success",
    )
