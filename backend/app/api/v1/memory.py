"""
记忆管理路由 — 单一职责：用户偏好/纠错/工作记忆清理的 HTTP 入口（B9）。

此前 Mem0Manager.correct_fact / MemoryManager.set_preference /
clear_working_memory 只有测试调用方，用户无法管理自己的记忆，
Graphiti 偏好时间线也永不写入。本路由补齐生产入口。

遵循分层架构：本模块仅做 HTTP 转换与属主校验，记忆读写委托
MemoryManager / Mem0Manager（租户边界由会话 GUC + RLS 兜底）。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.memory.mem0_manager import Mem0Manager
from app.memory.memory_manager import MemoryManager
from app.models.memory import MemoryFact
from app.models.user import User
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/memory", tags=["记忆管理"])


class PreferenceBody(BaseModel):
    """设置偏好的请求体。"""

    key: str = Field(..., min_length=1, max_length=100, description="偏好键")
    value: str = Field(..., min_length=1, max_length=500, description="偏好值")


class CorrectFactBody(BaseModel):
    """纠错请求体 — 两者皆 None 时仅停用旧事实。"""

    corrected_text: str | None = Field(default=None, description="纠正后的事实文本")
    corrected_value: str | None = Field(default=None, description="纠正后的结构化值")


@router.put("/preferences", response_model=ApiResponse[dict])
async def set_preference(
    request: Request,
    body: PreferenceBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """设置用户偏好 — 写 Mem0 当前事实 + Graphiti 变更时间线。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    memory = MemoryManager(db, tenant_id=tenant_id)
    fact = await memory.set_preference(user.id, body.key, body.value)
    return ApiResponse(
        code=0,
        data={"fact_id": str(fact.id), "key": body.key, "value": body.value},
        message="success",
    )


@router.post("/facts/{fact_id}/correct", response_model=ApiResponse[dict])
async def correct_fact(
    request: Request,
    fact_id: UUID,
    body: CorrectFactBody,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """纠正一条事实 — 旧事实停用（corrected 退场），可选写入新事实。

    属主校验：只能纠正自己的事实；同租户他人事实返回 404，
    不泄露存在性（correct_fact 本身不做属主检查，由此处把门）。
    """
    result = await db.execute(
        select(MemoryFact).where(
            MemoryFact.id == fact_id,
            MemoryFact.user_id == user.id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="事实不存在或不属于当前用户")

    tenant_id = getattr(request.state, "tenant_id", None)
    mem0 = Mem0Manager(db, tenant_id=tenant_id)
    new_fact = await mem0.correct_fact(
        fact_id,
        corrected_text=body.corrected_text,
        corrected_value=body.corrected_value,
    )
    return ApiResponse(
        code=0,
        data={
            "deactivated_fact_id": str(fact_id),
            "corrected_fact_id": str(new_fact.id) if new_fact else None,
        },
        message="success",
    )


@router.delete("/working-memory", response_model=ApiResponse[dict])
async def clear_working_memory(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ApiResponse[dict]:
    """清空当前用户的工作记忆（任务完成后调用），长期偏好不受影响。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    memory = MemoryManager(db, tenant_id=tenant_id)
    count = await memory.clear_working_memory(user.id)
    return ApiResponse(code=0, data={"cleared_count": count}, message="success")