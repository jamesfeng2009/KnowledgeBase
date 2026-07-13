"""
搜索路由 — 单一职责：处理搜索与索引重建的 HTTP 请求/响应转换。

遵循分层架构：本模块仅做 HTTP 路由和请求/响应序列化，
业务逻辑（检索引擎调用、权限过滤）委托给 SearchService。
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.search import (
    ReindexRequest,
    ReindexResponse,
    SearchResult,
    SearchResponse,
    SearchSuggestion,
    SearchType,
)
from app.services.search_service import SearchService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["搜索"])


@router.get("/search", response_model=ApiResponse[SearchResponse])
async def search(
    q: str = Query(..., min_length=1, max_length=1000, description="查询词"),
    kb_ids: str | None = Query(
        default=None,
        description="知识库 ID 列表（逗号分隔，如 'uuid1,uuid2'）",
    ),
    search_type: SearchType = Query(
        default=SearchType.hybrid, description="搜索类型"
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[SearchResponse]:
    """混合搜索（全文 + 向量），支持知识库过滤。

    搜索类型：
    - fulltext: 仅全文检索（基于 OpenSearch / PostgreSQL ILIKE）；
    - vector: 仅向量检索（基于 Milvus）；
    - hybrid: 混合检索（全文 + 向量 + Rerank 重排序）。
    """
    # 解析 kb_ids 参数
    parsed_kb_ids: list[UUID] | None = None
    if kb_ids:
        try:
            parsed_kb_ids = [UUID(kid.strip()) for kid in kb_ids.split(",") if kid.strip()]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="kb_ids 参数格式错误，应为逗号分隔的 UUID",
            )

    service = SearchService(db, user)
    result = await service.search(
        query=q,
        kb_ids=parsed_kb_ids,
        search_type=search_type,
        page=page,
        page_size=page_size,
    )

    return ApiResponse(
        code=0,
        data=SearchResponse(
            results=[SearchResult(**r) for r in result["results"]],
            total=result["total"],
            query=result["query"],
        ),
        message="success",
    )


@router.get("/search/suggest", response_model=ApiResponse[list[SearchSuggestion]])
async def search_suggest(
    q: str = Query(..., min_length=1, max_length=200, description="输入文本"),
    limit: int = Query(default=10, ge=1, le=50, description="返回建议数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[list[SearchSuggestion]]:
    """搜索建议（自动补全）。

    基于文档标题前缀匹配，返回用户可能想搜索的完整词。
    """
    service = SearchService(db, user)
    suggestions = await service.suggest(q, limit=limit)

    return ApiResponse(
        code=0,
        data=[SearchSuggestion(**s) for s in suggestions],
        message="success",
    )


@router.post("/search/reindex", response_model=ApiResponse[ReindexResponse])
async def reindex(
    body: ReindexRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[ReindexResponse]:
    """重建索引（仅 admin 权限）。

    触发异步索引重建任务，返回任务 ID 供轮询查询状态。
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可重建索引",
        )

    service = SearchService(db, user)
    task_id = await service.reindex(kb_ids=body.kb_ids, force=body.force)

    return ApiResponse(
        code=0,
        data=ReindexResponse(
            task_id=task_id,
            status="queued",
            message="索引重建任务已提交",
        ),
        message="success",
    )


@router.get("/search/unified")
async def unified_search(
    q: str = Query(..., min_length=1, max_length=1000, description="查询词"),
    sources: str | None = Query(
        default=None,
        description="搜索源列表（逗号分隔，如 'knowledge_base,oa,erp'），为空搜索全部",
    ),
    top_k: int = Query(default=20, ge=1, le=50, description="每个源返回数量"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse:
    """跨系统统一搜索 — 并行搜索知识库 + 外部系统。

    支持的搜索源：
    - knowledge_base: 知识库内部检索（全文+向量）
    - oa: OA 审批文档
    - erp: ERP 记录
    - crm: CRM 客户
    - mail: 邮件

    外部系统搜索结果依赖连接器是否已启用。
    """
    service = SearchService(db, user)
    parsed_sources = None
    if sources:
        parsed_sources = [s.strip() for s in sources.split(",") if s.strip()]

    result = await service.unified_search(
        query=q,
        sources=parsed_sources,
        top_k=top_k,
    )
    return ApiResponse(code=0, data=result, message="success")
