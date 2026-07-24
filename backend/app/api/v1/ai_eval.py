"""
AI 评测 API — 单一职责：处理 Prompt Injection 防御测试与 RAG 检索质量评测的 HTTP 请求/响应转换。

端点前缀：/testing/ai-eval
完整路径：/api/v1/testing/ai-eval/*
    - /injection/*   Prompt Injection 防御测试
    - /rag/*         RAG 检索质量评测（Recall@K / MRR / NDCG / MAP）

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑委托给 InjectionTestService / RagEvalService。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.ai_eval import (
    DocParseCaseCreate,
    DocParseCaseResponse,
    DocParseDatasetCreate,
    DocParseDatasetResponse,
    DocParseRunRequest,
    DocParseStatsResponse,
    InjectionCaseResultResponse,
    InjectionRunRequest,
    InjectionStatsResponse,
    InjectionSuiteCreate,
    InjectionSuiteResponse,
    JudgeCaseBatchCreate,
    JudgeCaseCreate,
    JudgeCaseResponse,
    JudgeDatasetCreate,
    JudgeDatasetResponse,
    JudgeRunRequest,
    JudgeStatsResponse,
    RagDatasetCreate,
    RagDatasetResponse,
    RagQueryCreate,
    RagQueryResponse,
    RagQueryResultResponse,
    RagRunRequest,
    RagStatsResponse,
)
from app.schemas.common import ApiResponse, PageResponse
from app.services.ai_eval.doc_parse_service import DocParseService
from app.services.ai_eval.injection_test_service import InjectionTestService
from app.services.ai_eval.injection_vectors import (
    get_attack_type_summary,
    get_preset_cases,
)
from app.services.ai_eval.judge_service import (
    DEFAULT_DIMENSIONS,
    DIMENSION_NAMES,
    JudgeService,
)
from app.services.ai_eval.rag_eval_queries import (
    QUERY_TYPE_NAMES,
    get_preset_queries,
    get_query_type_summary,
)
from app.services.ai_eval.rag_eval_service import RagEvalService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/testing/ai-eval", tags=["AI 评测"])


# ======================================================================
# 测试套件管理
# ======================================================================


@router.post(
    "/injection/suites",
    response_model=ApiResponse[InjectionSuiteResponse],
    status_code=201,
)
async def create_suite(
    body: InjectionSuiteCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[InjectionSuiteResponse]:
    """创建 Prompt Injection 测试套件。"""
    service = InjectionTestService(db)
    suite = await service.create_suite(
        name=body.name,
        user_id=user.id,
        description=body.description,
        target_mode=body.target_mode,
        kb_ids=body.kb_ids,
        tenant_id=getattr(user, "tenant_id", None),
    )
    # 自动导入预置用例
    await service.import_preset_cases(suite.id)

    await db.refresh(suite)

    return ApiResponse(
        code=0,
        data=InjectionSuiteResponse.model_validate(suite),
        message="测试套件创建成功，已自动导入预置攻击用例",
    )


@router.get(
    "/injection/suites",
    response_model=ApiResponse[PageResponse[InjectionSuiteResponse]],
)
async def list_suites(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[InjectionSuiteResponse]]:
    """分页查询测试套件列表。"""
    from sqlalchemy import func, select

    from app.models.ai_eval import InjectionTestSuite

    # 总数
    total = await db.scalar(
        select(func.count())
        .select_from(InjectionTestSuite)
        .where(InjectionTestSuite.deleted_at.is_(None))
    )

    # 分页查询
    result = await db.execute(
        select(InjectionTestSuite)
        .where(InjectionTestSuite.deleted_at.is_(None))
        .order_by(InjectionTestSuite.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    suites = result.scalars().all()

    return ApiResponse(
        code=0,
        data=PageResponse(
            items=[InjectionSuiteResponse.model_validate(s) for s in suites],
            total=total or 0,
            page=page,
            size=size,
            pages=(total or 0 + size - 1) // size,
        ),
    )


@router.get(
    "/injection/suites/{suite_id}",
    response_model=ApiResponse[InjectionSuiteResponse],
)
async def get_suite(
    suite_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[InjectionSuiteResponse]:
    """获取测试套件详情。"""
    from app.models.ai_eval import InjectionTestSuite

    suite = await db.get(InjectionTestSuite, suite_id)
    if suite is None or suite.deleted_at is not None:
        return ApiResponse(code=404, data=None, message="套件不存在")

    return ApiResponse(
        code=0,
        data=InjectionSuiteResponse.model_validate(suite),
    )


# ======================================================================
# 执行测试
# ======================================================================


@router.post(
    "/injection/run",
    response_model=ApiResponse,
)
async def run_suite(
    body: InjectionRunRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """执行 Prompt Injection 测试套件。

    将依次发送每条攻击用例到 LLM，收集响应并评分。
    执行完成后返回汇总统计。
    """
    service = InjectionTestService(db)
    try:
        summary = await service.run_suite(body.suite_id)
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))

    return ApiResponse(
        code=0,
        data=summary,
        message=f"测试完成：{summary['passed']} 通过, "
                f"{summary['partial']} 部分通过, "
                f"{summary['failed']} 失败",
    )


# ======================================================================
# 测试结果
# ======================================================================


@router.get(
    "/injection/suites/{suite_id}/results",
    response_model=ApiResponse[list[InjectionCaseResultResponse]],
)
async def get_suite_results(
    suite_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[InjectionCaseResultResponse]]:
    """获取套件下所有用例的执行结果。"""
    service = InjectionTestService(db)
    results = await service.get_suite_results(suite_id)

    return ApiResponse(
        code=0,
        data=results,
    )


# ======================================================================
# 统计
# ======================================================================


@router.get(
    "/injection/stats",
    response_model=ApiResponse[InjectionStatsResponse],
)
async def get_stats(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[InjectionStatsResponse]:
    """获取 Prompt Injection 测试全局统计。"""
    service = InjectionTestService(db)
    stats = await service.get_stats()

    return ApiResponse(
        code=0,
        data=InjectionStatsResponse(**stats),
    )


# ======================================================================
# 预置用例库信息
# ======================================================================


@router.get(
    "/injection/preset-cases",
    response_model=ApiResponse,
)
async def get_preset_cases_info(
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """获取预置攻击用例库信息（类型 + 数量）。"""
    cases = get_preset_cases()
    type_summary = get_attack_type_summary()

    return ApiResponse(
        code=0,
        data={
            "total_cases": len(cases),
            "by_type": type_summary,
            "cases": [
                {
                    "attack_type": c["attack_type"],
                    "severity": c["severity"],
                    "title": c["title"],
                    "expected_behavior": c["expected_behavior"],
                }
                for c in cases
            ],
        },
    )


# ======================================================================
# RAG 检索质量评测 — 数据集管理
# ======================================================================


@router.post(
    "/rag/datasets",
    response_model=ApiResponse[RagDatasetResponse],
    status_code=201,
)
async def create_rag_dataset(
    body: RagDatasetCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[RagDatasetResponse]:
    """创建 RAG 检索质量评测数据集。"""
    service = RagEvalService(db)
    dataset = await service.create_dataset(
        name=body.name,
        user_id=user.id,
        description=body.description,
        kb_ids=body.kb_ids,
        top_k=body.top_k,
        tenant_id=getattr(user, "tenant_id", None),
    )
    await db.refresh(dataset)
    return ApiResponse(
        code=0,
        data=RagDatasetResponse.model_validate(dataset),
        message="评测数据集创建成功",
    )


@router.get(
    "/rag/datasets",
    response_model=ApiResponse[PageResponse[RagDatasetResponse]],
)
async def list_rag_datasets(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[RagDatasetResponse]]:
    """分页查询 RAG 评测数据集列表。"""
    from app.models.ai_eval import RagEvalDataset

    total = await db.scalar(
        select(func.count())
        .select_from(RagEvalDataset)
        .where(RagEvalDataset.deleted_at.is_(None))
    )
    result = await db.execute(
        select(RagEvalDataset)
        .where(RagEvalDataset.deleted_at.is_(None))
        .order_by(RagEvalDataset.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    datasets = result.scalars().all()

    return ApiResponse(
        code=0,
        data=PageResponse(
            items=[RagDatasetResponse.model_validate(d) for d in datasets],
            total=total or 0,
            page=page,
            size=size,
            pages=((total or 0) + size - 1) // size,
        ),
    )


@router.get(
    "/rag/datasets/{dataset_id}",
    response_model=ApiResponse[RagDatasetResponse],
)
async def get_rag_dataset(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[RagDatasetResponse]:
    """获取 RAG 评测数据集详情。"""
    service = RagEvalService(db)
    dataset = await service.get_dataset(dataset_id)
    if dataset is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    return ApiResponse(
        code=0,
        data=RagDatasetResponse.model_validate(dataset),
    )


@router.delete(
    "/rag/datasets/{dataset_id}",
    response_model=ApiResponse,
)
async def delete_rag_dataset(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """删除 RAG 评测数据集（软删除）。"""
    service = RagEvalService(db)
    ok = await service.delete_dataset(dataset_id)
    if not ok:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    return ApiResponse(code=0, data=None, message="数据集已删除")


# ======================================================================
# RAG 评测 — 查询管理
# ======================================================================


@router.post(
    "/rag/datasets/{dataset_id}/queries",
    response_model=ApiResponse[RagQueryResponse],
    status_code=201,
)
async def add_rag_query(
    dataset_id: UUID,
    body: RagQueryCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[RagQueryResponse]:
    """添加一条评测查询（含人工标注的相关文档）。"""
    service = RagEvalService(db)
    if await service.get_dataset(dataset_id) is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    q = await service.add_query(
        dataset_id=dataset_id,
        query=body.query,
        ground_truth_doc_ids=body.ground_truth_doc_ids,
        query_type=body.query_type,
        difficulty=body.difficulty,
        expected_answer=body.expected_answer,
    )
    await db.refresh(q)
    return ApiResponse(
        code=0,
        data=RagQueryResponse.model_validate(q),
        message="评测查询已添加",
    )


@router.get(
    "/rag/datasets/{dataset_id}/queries",
    response_model=ApiResponse[list[RagQueryResponse]],
)
async def list_rag_queries(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[RagQueryResponse]]:
    """列出数据集下所有评测查询。"""
    service = RagEvalService(db)
    queries = await service.list_queries(dataset_id)
    return ApiResponse(
        code=0,
        data=[RagQueryResponse.model_validate(q) for q in queries],
    )


@router.delete(
    "/rag/queries/{query_id}",
    response_model=ApiResponse,
)
async def delete_rag_query(
    query_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """删除一条评测查询。"""
    service = RagEvalService(db)
    ok = await service.delete_query(query_id)
    if not ok:
        return ApiResponse(code=404, data=None, message="查询不存在")
    return ApiResponse(code=0, data=None, message="查询已删除")


@router.post(
    "/rag/datasets/{dataset_id}/queries/import-preset",
    response_model=ApiResponse,
)
async def import_preset_rag_queries(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """导入预置查询模板（12 条，6 类检索场景）。

    注意：预置模板不含 ground_truth，导入后须手动标注相关文档才能评测。
    """
    service = RagEvalService(db)
    if await service.get_dataset(dataset_id) is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    queries = await service.import_preset_queries(dataset_id)
    return ApiResponse(
        code=0,
        data={"imported": len(queries)},
        message=f"已导入 {len(queries)} 条预置查询模板，请前往标注相关文档",
    )


# ======================================================================
# RAG 评测 — 执行与结果
# ======================================================================


@router.post(
    "/rag/run",
    response_model=ApiResponse,
)
async def run_rag_dataset(
    body: RagRunRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """执行 RAG 检索质量评测。

    对每条已标注查询调用生产级混合检索器检索，按 doc_id 去重保序后
    计算 Recall@K / Precision@K / MRR / NDCG@K / MAP 指标。
    """
    service = RagEvalService(db)
    try:
        summary = await service.run_dataset(body.dataset_id, top_k=body.top_k)
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))

    return ApiResponse(
        code=0,
        data=summary,
        message=(
            f"评测完成：{summary['executed']} 条查询已评估，"
            f"命中率 {summary['hit_rate']:.1%}，"
            f"平均 Recall@5 {summary['avg_recall_at_5']:.1%}，"
            f"平均 MRR {summary['avg_mrr']:.3f}"
        ),
    )


@router.get(
    "/rag/datasets/{dataset_id}/results",
    response_model=ApiResponse[list[RagQueryResultResponse]],
)
async def get_rag_results(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[RagQueryResultResponse]]:
    """获取数据集下所有查询的检索结果与指标。"""
    service = RagEvalService(db)
    results = await service.get_dataset_results(dataset_id)
    return ApiResponse(code=0, data=results)


# ======================================================================
# RAG 评测 — 统计与预置查询
# ======================================================================


@router.get(
    "/rag/stats",
    response_model=ApiResponse[RagStatsResponse],
)
async def get_rag_stats(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[RagStatsResponse]:
    """获取 RAG 评测全局统计。"""
    service = RagEvalService(db)
    stats = await service.get_stats()
    return ApiResponse(
        code=0,
        data=RagStatsResponse(**stats),
    )


@router.get(
    "/rag/preset-queries",
    response_model=ApiResponse,
)
async def get_preset_queries_info(
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """获取预置查询模板库信息（类型 + 数量）。"""
    queries = get_preset_queries()
    type_summary = get_query_type_summary()
    return ApiResponse(
        code=0,
        data={
            "total_queries": len(queries),
            "by_type": type_summary,
            "type_names": QUERY_TYPE_NAMES,
            "queries": [
                {
                    "query": q["query"],
                    "query_type": q["query_type"],
                    "difficulty": q["difficulty"],
                    "description": q["description"],
                }
                for q in queries
            ],
        },
    )


# ======================================================================
# 文档解析评测 — 数据集管理
# ======================================================================


@router.post(
    "/doc-parse/datasets",
    response_model=ApiResponse[DocParseDatasetResponse],
    status_code=201,
)
async def create_doc_parse_dataset(
    body: DocParseDatasetCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocParseDatasetResponse]:
    """创建文档解析评测数据集。"""
    service = DocParseService(db)
    dataset = await service.create_dataset(
        name=body.name,
        user_id=user.id,
        description=body.description,
        tenant_id=getattr(user, "tenant_id", None),
    )
    await db.refresh(dataset)
    return ApiResponse(
        code=0,
        data=DocParseDatasetResponse.model_validate(dataset),
        message="解析评测数据集创建成功",
    )


@router.get(
    "/doc-parse/datasets",
    response_model=ApiResponse[PageResponse[DocParseDatasetResponse]],
)
async def list_doc_parse_datasets(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[DocParseDatasetResponse]]:
    """分页查询文档解析评测数据集列表。"""
    from app.models.ai_eval import DocParseDataset

    total = await db.scalar(
        select(func.count())
        .select_from(DocParseDataset)
        .where(DocParseDataset.deleted_at.is_(None))
    )
    result = await db.execute(
        select(DocParseDataset)
        .where(DocParseDataset.deleted_at.is_(None))
        .order_by(DocParseDataset.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    datasets = result.scalars().all()
    return ApiResponse(
        code=0,
        data=PageResponse(
            items=[DocParseDatasetResponse.model_validate(d) for d in datasets],
            total=total or 0,
            page=page,
            size=size,
            pages=((total or 0) + size - 1) // size,
        ),
    )


@router.get(
    "/doc-parse/datasets/{dataset_id}",
    response_model=ApiResponse[DocParseDatasetResponse],
)
async def get_doc_parse_dataset(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocParseDatasetResponse]:
    """获取文档解析评测数据集详情。"""
    service = DocParseService(db)
    dataset = await service.get_dataset(dataset_id)
    if dataset is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    return ApiResponse(
        code=0,
        data=DocParseDatasetResponse.model_validate(dataset),
    )


@router.delete(
    "/doc-parse/datasets/{dataset_id}",
    response_model=ApiResponse,
)
async def delete_doc_parse_dataset(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """删除文档解析评测数据集（软删除）。"""
    service = DocParseService(db)
    ok = await service.delete_dataset(dataset_id)
    if not ok:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    return ApiResponse(code=0, data=None, message="数据集已删除")


# ======================================================================
# 文档解析评测 — 用例管理
# ======================================================================


@router.post(
    "/doc-parse/datasets/{dataset_id}/cases",
    response_model=ApiResponse[DocParseCaseResponse],
    status_code=201,
)
async def add_doc_parse_case(
    dataset_id: UUID,
    body: DocParseCaseCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocParseCaseResponse]:
    """添加一条解析评测用例。

    两种模式：
        - 直接提供模式：expected_text + parsed_text
        - Docling 端到端模式：expected_text + document_id（执行评测时用 Docling 解析）
    """
    service = DocParseService(db)
    if await service.get_dataset(dataset_id) is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    if not body.parsed_text and not body.document_id:
        return ApiResponse(
            code=400,
            data=None,
            message="parsed_text 与 document_id 至少需要一个（直接提供模式或 Docling 端到端模式）",
        )
    case = await service.add_case(
        dataset_id=dataset_id,
        title=body.title,
        expected_text=body.expected_text,
        doc_type=body.doc_type,
        difficulty=body.difficulty,
        document_id=body.document_id,
        parsed_text=body.parsed_text,
    )
    await db.refresh(case)
    return ApiResponse(
        code=0,
        data=DocParseCaseResponse.model_validate(case),
        message="解析用例已添加",
    )


@router.get(
    "/doc-parse/datasets/{dataset_id}/cases",
    response_model=ApiResponse[list[DocParseCaseResponse]],
)
async def list_doc_parse_cases(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[DocParseCaseResponse]]:
    """列出数据集下所有解析评测用例。"""
    service = DocParseService(db)
    cases = await service.list_cases(dataset_id)
    return ApiResponse(
        code=0,
        data=[DocParseCaseResponse.model_validate(c) for c in cases],
    )


@router.delete(
    "/doc-parse/cases/{case_id}",
    response_model=ApiResponse,
)
async def delete_doc_parse_case(
    case_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """删除一条解析评测用例。"""
    service = DocParseService(db)
    ok = await service.delete_case(case_id)
    if not ok:
        return ApiResponse(code=404, data=None, message="用例不存在")
    return ApiResponse(code=0, data=None, message="用例已删除")


# ======================================================================
# 文档解析评测 — 执行与结果
# ======================================================================


@router.post(
    "/doc-parse/run",
    response_model=ApiResponse,
)
async def run_doc_parse_dataset(
    body: DocParseRunRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """执行文档解析评测。

    对每条用例计算文本相似度/表格/公式/版面四维度指标。
    Docling 端到端模式会下载文档并用 Docling 解析后对比标注。
    """
    service = DocParseService(db)
    try:
        summary = await service.run_dataset(body.dataset_id)
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))

    return ApiResponse(
        code=0,
        data=summary,
        message=(
            f"评测完成：{summary['executed']} 条用例已评估，"
            f"平均文本相似度 {summary['avg_text_similarity']:.1%}，"
            f"综合得分 {summary['avg_overall_score']:.1%}"
        ),
    )


@router.get(
    "/doc-parse/datasets/{dataset_id}/results",
    response_model=ApiResponse,
)
async def get_doc_parse_results(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """获取数据集下所有用例的解析结果与指标。"""
    service = DocParseService(db)
    results = await service.get_dataset_results(dataset_id)
    return ApiResponse(code=0, data=results)


@router.get(
    "/doc-parse/stats",
    response_model=ApiResponse[DocParseStatsResponse],
)
async def get_doc_parse_stats(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[DocParseStatsResponse]:
    """获取文档解析评测全局统计。"""
    service = DocParseService(db)
    stats = await service.get_stats()
    return ApiResponse(
        code=0,
        data=DocParseStatsResponse(**stats),
    )


# ======================================================================
# AI Judge 自动评测 — 数据集管理
# ======================================================================


@router.post(
    "/judge/datasets",
    response_model=ApiResponse[JudgeDatasetResponse],
    status_code=201,
)
async def create_judge_dataset(
    body: JudgeDatasetCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[JudgeDatasetResponse]:
    """创建 AI Judge 评测数据集。"""
    service = JudgeService(db)
    dataset = await service.create_dataset(
        name=body.name,
        user_id=user.id,
        description=body.description,
        judge_model=body.judge_model,
        dimensions=body.dimensions,
        tenant_id=getattr(user, "tenant_id", None),
    )
    await db.refresh(dataset)
    return ApiResponse(
        code=0,
        data=JudgeDatasetResponse.model_validate(dataset),
        message="Judge 评测数据集创建成功",
    )


@router.get(
    "/judge/datasets",
    response_model=ApiResponse[PageResponse[JudgeDatasetResponse]],
)
async def list_judge_datasets(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[JudgeDatasetResponse]]:
    """分页查询 Judge 评测数据集列表。"""
    from app.models.ai_eval import JudgeDataset

    total = await db.scalar(
        select(func.count())
        .select_from(JudgeDataset)
        .where(JudgeDataset.deleted_at.is_(None))
    )
    result = await db.execute(
        select(JudgeDataset)
        .where(JudgeDataset.deleted_at.is_(None))
        .order_by(JudgeDataset.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    datasets = result.scalars().all()
    return ApiResponse(
        code=0,
        data=PageResponse(
            items=[JudgeDatasetResponse.model_validate(d) for d in datasets],
            total=total or 0,
            page=page,
            size=size,
            pages=((total or 0) + size - 1) // size,
        ),
    )


@router.get(
    "/judge/datasets/{dataset_id}",
    response_model=ApiResponse[JudgeDatasetResponse],
)
async def get_judge_dataset(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[JudgeDatasetResponse]:
    """获取 Judge 评测数据集详情。"""
    service = JudgeService(db)
    dataset = await service.get_dataset(dataset_id)
    if dataset is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    return ApiResponse(
        code=0,
        data=JudgeDatasetResponse.model_validate(dataset),
    )


@router.delete(
    "/judge/datasets/{dataset_id}",
    response_model=ApiResponse,
)
async def delete_judge_dataset(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """删除 Judge 评测数据集（软删除）。"""
    service = JudgeService(db)
    ok = await service.delete_dataset(dataset_id)
    if not ok:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    return ApiResponse(code=0, data=None, message="数据集已删除")


# ======================================================================
# AI Judge 评测 — 用例管理（含批量导入，用于回归评测）
# ======================================================================


@router.post(
    "/judge/datasets/{dataset_id}/cases",
    response_model=ApiResponse[JudgeCaseResponse],
    status_code=201,
)
async def add_judge_case(
    dataset_id: UUID,
    body: JudgeCaseCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[JudgeCaseResponse]:
    """添加一条 Judge 评测用例。"""
    service = JudgeService(db)
    if await service.get_dataset(dataset_id) is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    case = await service.add_case(
        dataset_id=dataset_id,
        question=body.question,
        reference_answer=body.reference_answer,
        model_answer=body.model_answer,
        category=body.category,
    )
    await db.refresh(case)
    return ApiResponse(
        code=0,
        data=JudgeCaseResponse.model_validate(case),
        message="Judge 用例已添加",
    )


@router.post(
    "/judge/datasets/{dataset_id}/cases/batch",
    response_model=ApiResponse,
)
async def add_judge_cases_batch(
    dataset_id: UUID,
    body: JudgeCaseBatchCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """批量添加 Judge 用例（用于批量回归评测）。

    一次提交多条 question/reference_answer/model_answer，便于构建回归测试集。
    """
    service = JudgeService(db)
    if await service.get_dataset(dataset_id) is None:
        return ApiResponse(code=404, data=None, message="数据集不存在")
    cases_payload = [
        {
            "question": c.question,
            "reference_answer": c.reference_answer,
            "model_answer": c.model_answer,
            "category": c.category,
        }
        for c in body.cases
    ]
    added = await service.add_cases_batch(dataset_id, cases_payload)
    return ApiResponse(
        code=0,
        data={"imported": len(added)},
        message=f"已批量添加 {len(added)} 条 Judge 用例",
    )


@router.get(
    "/judge/datasets/{dataset_id}/cases",
    response_model=ApiResponse[list[JudgeCaseResponse]],
)
async def list_judge_cases(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[JudgeCaseResponse]]:
    """列出数据集下所有 Judge 评测用例。"""
    service = JudgeService(db)
    cases = await service.list_cases(dataset_id)
    return ApiResponse(
        code=0,
        data=[JudgeCaseResponse.model_validate(c) for c in cases],
    )


@router.delete(
    "/judge/cases/{case_id}",
    response_model=ApiResponse,
)
async def delete_judge_case(
    case_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """删除一条 Judge 评测用例。"""
    service = JudgeService(db)
    ok = await service.delete_case(case_id)
    if not ok:
        return ApiResponse(code=404, data=None, message="用例不存在")
    return ApiResponse(code=0, data=None, message="用例已删除")


# ======================================================================
# AI Judge 评测 — 执行与结果
# ======================================================================


@router.post(
    "/judge/run",
    response_model=ApiResponse,
)
async def run_judge_dataset(
    body: JudgeRunRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """执行 AI Judge 批量回归评测。

    对每条用例构造裁判提示词 → 调用 LLM → 解析 JSON 评分 → 汇总各维度均值。
    """
    service = JudgeService(db)
    try:
        summary = await service.run_dataset(body.dataset_id)
    except ValueError as exc:
        return ApiResponse(code=404, data=None, message=str(exc))

    return ApiResponse(
        code=0,
        data=summary,
        message=(
            f"裁判评测完成：{summary['executed']} 条用例已评分，"
            f"平均综合分 {summary['avg_overall']:.1f}"
        ),
    )


@router.get(
    "/judge/datasets/{dataset_id}/results",
    response_model=ApiResponse,
)
async def get_judge_results(
    dataset_id: UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """获取数据集下所有用例的裁判评分结果。"""
    service = JudgeService(db)
    results = await service.get_dataset_results(dataset_id)
    return ApiResponse(code=0, data=results)


@router.get(
    "/judge/stats",
    response_model=ApiResponse[JudgeStatsResponse],
)
async def get_judge_stats(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[JudgeStatsResponse]:
    """获取 AI Judge 评测全局统计。"""
    service = JudgeService(db)
    stats = await service.get_stats()
    return ApiResponse(
        code=0,
        data=JudgeStatsResponse(**stats),
    )


@router.get(
    "/judge/dimensions",
    response_model=ApiResponse,
)
async def get_judge_dimensions_info(
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """获取 Judge 评分维度信息（供前端创建数据集时选择维度）。"""
    return ApiResponse(
        code=0,
        data={
            "default_dimensions": DEFAULT_DIMENSIONS,
            "dimension_names": DIMENSION_NAMES,
        },
    )
