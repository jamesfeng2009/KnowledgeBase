"""交付链视图 API — P2b 只读端点：Intent → Process → Output → 反馈 → 沉淀。

数据源为只读视图 ``v_delivery_chain``（一答一行，按 assistant 消息锚定）：
    - Intent    同会话紧邻的前一条 user 消息
    - Process   tool_audit_log / agent_event_logs 聚合（时间窗约束）
    - Output    引用卡片 / 摘要 / 模型
    - 反馈      feedbacks（praise/complaint/bug/suggestion）
    - 沉淀      knowledge_assets（经 chat_feedback 反馈回溯）

本模块仅做 HTTP 路由与序列化，业务查询直接走视图 SQL。
权限：仅 admin。租户隔离：request.state.tenant_id 注入查询条件。
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/delivery-chain", tags=["交付链视图"])

#: 明细查询的过程窗口（与视图聚合窗口一致）
_DETAIL_WINDOW_SQL = "AND created_at >= :win_start AND created_at <= :win_end"


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可查看交付链",
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    """视图行 → 字典（JSON 友好）。"""
    return {
        "message_id": str(row.message_id),
        "conversation_id": str(row.conversation_id),
        "tenant_id": str(row.tenant_id) if row.tenant_id else None,
        "conversation_title": row.conversation_title,
        "answered_at": row.answered_at.isoformat() if row.answered_at else None,
        "user_message_id": str(row.user_message_id) if row.user_message_id else None,
        "user_question": row.user_question,
        "tool_calls": int(row.tool_calls or 0),
        "tool_errors": int(row.tool_errors or 0),
        "tool_names": list(row.tool_names or []),
        "node_runs": int(row.node_runs or 0),
        "max_iteration": int(row.max_iteration or 0),
        "total_latency_ms": int(row.total_latency_ms) if row.total_latency_ms is not None else None,
        "total_tokens": int(row.total_tokens) if row.total_tokens is not None else None,
        "has_error": bool(row.has_error),
        "answer_excerpt": row.answer_excerpt,
        "citations": row.citations,
        "model_used": row.model_used,
        "answer_tokens": int(row.answer_tokens or 0),
        "feedback_count": int(row.feedback_count or 0),
        "feedback_types": list(row.feedback_types or []),
        "is_badcase": bool(row.is_badcase),
        "sediment_asset_ids": [str(x) for x in (row.sediment_asset_ids or [])],
        "sediment_doc_ids": [str(x) for x in (row.sediment_doc_ids or [])],
    }


# ======================================================================
# 交付链列表
# ======================================================================


@router.get("", response_model=ApiResponse[PageResponse[dict]])
async def list_delivery_chain(
    request: Request,
    conversation_id: uuid.UUID | None = Query(None, description="按会话过滤"),
    feedback_type: str | None = Query(
        None, description="按反馈类型过滤（praise/complaint/bug/suggestion）"
    ),
    is_badcase: bool | None = Query(None, description="仅看 badcase（投诉/缺陷反馈）"),
    date_from: datetime | None = Query(None, description="回答时间起（UTC）"),
    date_to: datetime | None = Query(None, description="回答时间止（UTC）"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResponse[dict]]:
    """分页查询交付链视图（只读）。

    权限：仅管理员。强制租户过滤；列表查询始终按回答时间倒序。
    """
    _require_admin(user)
    tenant_id = getattr(request.state, "tenant_id", None)

    conditions = ["(:tid::uuid IS NULL OR tenant_id = :tid::uuid)"]
    params: dict[str, Any] = {"tid": tenant_id}
    if conversation_id is not None:
        conditions.append("conversation_id = :conversation_id::uuid")
        params["conversation_id"] = str(conversation_id)
    if feedback_type:
        conditions.append(":ftype::text = ANY(feedback_types)")
        params["ftype"] = feedback_type
    if is_badcase is not None:
        conditions.append("is_badcase = :is_badcase")
        params["is_badcase"] = is_badcase
    if date_from is not None:
        conditions.append("answered_at >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        conditions.append("answered_at <= :date_to")
        params["date_to"] = date_to
    where_clause = " AND ".join(conditions)

    count_sql = f"SELECT count(*) AS total FROM v_delivery_chain WHERE {where_clause}"
    total = (await db.execute(text(count_sql), params)).scalar() or 0

    list_sql = (
        f"SELECT * FROM v_delivery_chain WHERE {where_clause} "
        "ORDER BY answered_at DESC LIMIT :limit OFFSET :offset"
    )
    params["limit"] = size
    params["offset"] = (page - 1) * size
    rows = (await db.execute(text(list_sql), params)).fetchall()

    return ApiResponse(
        data=PageResponse(
            items=[_row_to_dict(r) for r in rows],
            total=int(total),
            page=page,
            size=size,
            pages=math.ceil(total / size) if size else 0,
        )
    )


# ======================================================================
# 交付链详情
# ======================================================================


@router.get("/{message_id}", response_model=ApiResponse[dict])
async def get_delivery_chain_detail(
    message_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """单条交付链详情 — 含工具调用明细与事件节点序列。

    权限：仅管理员。过程明细限定在回答时间 ±1 小时窗口内，
    事件按 seq、工具按 created_at 排序，分别截断（500 / 200）防超大响应。
    """
    _require_admin(user)
    tenant_id = getattr(request.state, "tenant_id", None)

    row = (
        await db.execute(
            text(
                "SELECT * FROM v_delivery_chain "
                "WHERE message_id = :mid::uuid "
                "AND (:tid::uuid IS NULL OR tenant_id = :tid::uuid)"
            ),
            {"mid": str(message_id), "tid": tenant_id},
        )
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="交付链不存在"
        )

    chain = _row_to_dict(row)
    conversation_id = chain["conversation_id"]
    # 过程明细时间窗：回答时刻 ±1 小时（与视图聚合窗口一致）
    answered_at = row.answered_at
    win_start = answered_at - timedelta(hours=1)
    win_end = answered_at + timedelta(hours=1)
    window_params = {
        "sid": conversation_id,
        "win_start": win_start,
        "win_end": win_end,
    }

    # Process 明细：工具调用（时间窗与视图聚合一致）
    tool_rows = (
        await db.execute(
            text(
                "SELECT id, run_id, span_id, tool_name, status, duration_ms, "
                "error, result_summary, evidence_ref, created_at "
                "FROM tool_audit_log "
                "WHERE session_id = :sid::text "
                f"{_DETAIL_WINDOW_SQL} "
                "ORDER BY created_at LIMIT 200"
            ),
            window_params,
        )
    ).fetchall()
    chain["tool_calls_detail"] = [
        {
            "id": str(r.id),
            "run_id": str(r.run_id) if r.run_id else None,
            "span_id": str(r.span_id) if r.span_id else None,
            "tool_name": r.tool_name,
            "status": r.status,
            "duration_ms": int(r.duration_ms or 0),
            "error": r.error,
            "result_summary": r.result_summary,
            "evidence_ref": r.evidence_ref,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in tool_rows
    ]

    # Process 明细：Agent 事件节点序列
    event_rows = (
        await db.execute(
            text(
                "SELECT seq, event_type, node_name, iteration, metadata, created_at "
                "FROM agent_event_logs "
                "WHERE session_id = :sid::text "
                f"{_DETAIL_WINDOW_SQL} "
                "ORDER BY seq LIMIT 500"
            ),
            window_params,
        )
    ).fetchall()
    chain["event_nodes"] = [
        {
            "seq": r.seq,
            "event_type": r.event_type,
            "node_name": r.node_name,
            "iteration": int(r.iteration or 0),
            "metadata": r.metadata,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in event_rows
    ]

    return ApiResponse(data=chain)
