"""
IT 工单 API — 单一职责：工单的 HTTP 端点。

P1 修复背景：``frontend/src/lib/apis/tickets.ts`` 一直在调
``/api/v1/tickets``（列表 / 创建 / 更新），``/scenes/it-helpdesk`` 场景页
依赖它渲染工单表格与「采纳 AI 解答」，但后端此前**完全没有**这个模块
（全仓无 router / model / service），页面一进去就是"工单列表加载失败"。

端点：
    GET    /tickets            列表（status / category 过滤 + 分页）
    POST   /tickets            创建工单
    GET    /tickets/{id}       详情
    PUT    /tickets/{id}       更新（状态流转受校验）
    POST   /tickets/{id}/resolve-by-ai   采纳 AI 解答并关闭
    DELETE /tickets/{id}       软删除

权限：所有端点要求登录用户；工单按 ``request.state.tenant_id`` 隔离。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.schemas.ticket import TicketCreate, TicketResponse, TicketUpdate
from app.services.ticket_service import InvalidTransitionError, TicketService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/tickets", tags=["IT 工单"])


def _service(request: Request, db: AsyncSession) -> TicketService:
    """按请求上下文的租户构造 Service。"""
    return TicketService(db, tenant_id=getattr(request.state, "tenant_id", None))


@router.get("", response_model=ApiResponse[PageResponse[TicketResponse]])
async def list_tickets(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status", description="状态过滤"),
    category: str | None = Query(default=None, description="分类过滤"),
    mine: bool = Query(default=False, description="只看我提交的工单"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[TicketResponse]]:
    """工单列表 — 前端工单表格数据源。"""
    service = _service(request, db)
    try:
        tickets, total = await service.list_tickets(
            status=status_filter,
            category=category,
            page=page,
            size=size,
            user_id=user.id if mine else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ApiResponse(
        code=0,
        data=PageResponse(
            items=[TicketResponse.model_validate(t) for t in tickets],
            total=total,
            page=page,
            size=size,
            pages=(total + size - 1) // size if size else 0,
        ),
        message="success",
    )


@router.post("", response_model=ApiResponse[TicketResponse], status_code=201)
async def create_ticket(
    request: Request,
    body: TicketCreate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TicketResponse]:
    """创建工单 — 提交人 = 当前登录用户。"""
    service = _service(request, db)
    ticket = await service.create_ticket(
        user=user,
        title=body.title,
        description=body.description,
        category=body.category,
        priority=body.priority,
    )
    await db.commit()
    return ApiResponse(
        code=0,
        data=TicketResponse.model_validate(ticket),
        message="success",
    )


@router.get("/{ticket_id}", response_model=ApiResponse[TicketResponse])
async def get_ticket(
    request: Request,
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TicketResponse]:
    """工单详情。"""
    ticket = await _service(request, db).get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ApiResponse(
        code=0,
        data=TicketResponse.model_validate(ticket),
        message="success",
    )


@router.put("/{ticket_id}", response_model=ApiResponse[TicketResponse])
async def update_ticket(
    request: Request,
    ticket_id: uuid.UUID,
    body: TicketUpdate,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TicketResponse]:
    """更新工单 — 采纳 AI 解答时前端会同时传 ``status=resolved`` + ``ai_solved=true``。

    Raises:
        HTTPException: 404 工单不存在；409 状态流转非法。
    """
    service = _service(request, db)
    try:
        ticket = await service.update_ticket(
            ticket_id, **body.model_dump(exclude_unset=True)
        )
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        # Service 用 ValueError 表示"不存在"；显式映射成 404，
        # 不依赖 app.main 的全局处理器（子路由单独挂载时也要正确）
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    return ApiResponse(
        code=0,
        data=TicketResponse.model_validate(ticket),
        message="success",
    )


@router.post("/{ticket_id}/resolve-by-ai", response_model=ApiResponse[TicketResponse])
async def resolve_ticket_by_ai(
    request: Request,
    ticket_id: uuid.UUID,
    ai_answer: str | None = Query(default=None, description="AI 解答原文"),
    ai_solution: str | None = Query(default=None, description="结构化解决步骤"),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[TicketResponse]:
    """采纳 AI 解答并关闭工单（显式端点，语义比裸 PUT 清晰）。"""
    service = _service(request, db)
    try:
        ticket = await service.resolve_by_ai(ticket_id, ai_answer, ai_solution)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    return ApiResponse(
        code=0,
        data=TicketResponse.model_validate(ticket),
        message="success",
    )


@router.delete("/{ticket_id}", response_model=ApiResponse[dict])
async def delete_ticket(
    request: Request,
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """软删除工单。"""
    service = _service(request, db)
    try:
        await service.soft_delete(ticket_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    return ApiResponse(code=0, data={"id": str(ticket_id)}, message="success")
