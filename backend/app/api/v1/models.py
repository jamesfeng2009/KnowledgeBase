"""模型选择路由 — 单一职责：模型列表查询与会话级模型切换 REST 端点。

P2 核心：前端通过这些端点查询可用模型列表、设置会话级模型偏好。

端点：
    GET  /api/v1/models                    — 获取当前部署模式可用模型列表
    GET  /api/v1/models/session/{sess_id}  — 获取会话当前使用的模型
    PUT  /api/v1/models/session/{sess_id}  — 设置会话级模型选择
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import get_current_active_user
from app.models.user import User
from app.services.model_selection_service import ModelSelectionService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/models", tags=["模型选择"])


class ModelInfo(BaseModel):
    """模型信息（前端下拉框数据源）。"""

    id: str = Field(..., description="模型 ID")
    display_name: str = Field(..., description="显示名称")
    description: str = Field("", description="模型描述")
    tier: str = Field("standard", description="模型层级: premium/standard/lite")
    is_default: bool = Field(False, description="是否为默认模型")
    max_tokens: int = Field(0, description="上下文窗口大小")
    supports_vision: bool = Field(False, description="是否支持视觉")
    supports_tool_use: bool = Field(False, description="是否支持工具调用")


class ModelListResponse(BaseModel):
    """模型列表响应。"""

    models: list[ModelInfo] = Field(default_factory=list)
    current_model_id: str | None = Field(None, description="当前会话选中的模型 ID")


class SessionModelRequest(BaseModel):
    """设置会话模型请求。"""

    model_id: str = Field(..., description="模型 ID（从 GET /models 获取）")


class SessionModelResponse(BaseModel):
    """会话模型响应。"""

    session_id: str = Field(..., description="会话 ID")
    model_id: str = Field(..., description="当前使用的模型 ID")
    model_display_name: str = Field("", description="模型显示名称")


@router.get("")
async def list_models(
    request: Request,
    session_id: str | None = None,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> ModelListResponse:
    """获取当前部署模式可用模型列表。

    可选传入 session_id，返回时附带当前会话选中的模型 ID。

    Args:
        session_id: 可选，会话 ID。传入时响应包含 current_model_id。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ModelSelectionService(db, tenant_id=tenant_id)
    models = await service.get_available_models_for_user(user.id)

    current_model_id = None
    if session_id:
        current_model_id = await service.get_session_model(user.id, session_id)

    return ModelListResponse(
        models=[ModelInfo(**m) for m in models],
        current_model_id=current_model_id,
    )


@router.get("/session/{session_id}")
async def get_session_model(
    request: Request,
    session_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> SessionModelResponse:
    """获取会话当前使用的模型（两级优先级解析）。

    优先级：session 级（DB）> system 默认（models.json is_default）。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ModelSelectionService(db, tenant_id=tenant_id)
    model_id = await service.resolve_model(user.id, session_id)

    # 查找模型显示名称
    from app.llm.model_config import get_model_by_id

    model_config = get_model_by_id(model_id)
    display_name = model_config.get("display_name", "") if model_config else ""

    return SessionModelResponse(
        session_id=session_id,
        model_id=model_id,
        model_display_name=display_name,
    )


@router.put("/session/{session_id}")
async def set_session_model(
    request: Request,
    session_id: str,
    body: SessionModelRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
) -> SessionModelResponse:
    """设置会话级模型选择。

    将用户选择的模型持久化到 user_model_preferences 表，
    后续对话将使用该模型（而非系统默认）。

    Args:
        session_id: 会话 ID。
        body: 包含 model_id 的请求体。
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    service = ModelSelectionService(db, tenant_id=tenant_id)
    try:
        await service.set_session_model(user.id, session_id, body.model_id)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        log.error("models.set_session_model.error", error=str(exc))
        raise HTTPException(
            status_code=500, detail="设置模型失败，请稍后重试"
        ) from exc

    # 返回完整模型信息
    from app.llm.model_config import get_model_by_id

    model_config = get_model_by_id(body.model_id)
    display_name = model_config.get("display_name", "") if model_config else ""

    return SessionModelResponse(
        session_id=session_id,
        model_id=body.model_id,
        model_display_name=display_name,
    )
