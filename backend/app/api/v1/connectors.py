"""
连接器管理 API — 单一职责：管理外部系统连接器。

端点：
    GET  /connectors               — 连接器列表
    POST /connectors/{id}/test      — 测试连通性
    PUT  /connectors/{id}/toggle     — 启用/停用
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.deps import require_module
from app.models.user import User
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("")
async def list_connectors(
    user: User = Depends(require_module("unified_search")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """获取所有连接器列表及其状态。"""
    from app.connectors.registry import connector_registry

    connectors = connector_registry.list_connectors()
    return ApiResponse(code=0, data=connectors, message="success")


@router.post("/{connector_id}/test")
async def test_connector(
    connector_id: str,
    user: User = Depends(require_module("unified_search")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """测试指定连接器的连通性。"""
    if user.role not in ("admin", "kb_admin"):
        return ApiResponse(code=403, data=None, message="需要管理员权限")

    from app.connectors.registry import connector_registry

    connector = connector_registry.get(connector_id)
    if connector is None:
        return ApiResponse(code=404, data=None, message="连接器不存在")

    success = await connector.test_connection()
    return ApiResponse(
        code=0,
        data={"connector_id": connector_id, "connected": success},
        message="连接成功" if success else "连接失败",
    )


@router.put("/{connector_id}/toggle")
async def toggle_connector(
    connector_id: str,
    active: bool,
    user: User = Depends(require_module("unified_search")),
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse:
    """启用或停用连接器。"""
    if user.role not in ("admin",):
        return ApiResponse(code=403, data=None, message="仅管理员可操作")

    from app.connectors.registry import connector_registry

    success = connector_registry.toggle(connector_id, active)
    if not success:
        return ApiResponse(code=404, data=None, message="连接器不存在")

    return ApiResponse(
        code=0,
        data={"connector_id": connector_id, "is_active": active},
        message="success",
    )
