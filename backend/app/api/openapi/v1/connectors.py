"""企业系统连接器开放 API — 对接 OA/ERP/HR 系统。

为外部系统提供统一的查询入口，通过 API Key 的 scope 控制访问范围：
- ``connector:oa`` — OA 审批状态查询
- ``connector:erp`` — ERP 数据查询
- ``connector:hr`` — 员工信息查询

遵循单一职责：本模块仅做 HTTP 路由与请求转发，
具体企业系统对接逻辑由各自适配器实现（此处为预留接口）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.openapi.deps import require_scope
from app.schemas.common import ApiResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/connectors", tags=["开放接口-企业连接器"])


# ======================================================================
# 请求 Schema
# ======================================================================


class OAQueryRequest(BaseModel):
    """OA 审批查询请求。"""

    bill_no: str = Field(..., description="单据编号")
    system: str = Field(default="default", description="OA 系统标识")


class ERPQueryRequest(BaseModel):
    """ERP 数据查询请求。"""

    module: str = Field(..., description="ERP 模块（如 sales/purchase/finance）")
    filters: dict[str, Any] = Field(default_factory=dict, description="查询条件")


class HREmployeeRequest(BaseModel):
    """员工信息查询请求。"""

    employee_id: str | None = Field(default=None, description="员工工号")
    email: str | None = Field(default=None, description="员工邮箱")
    name: str | None = Field(default=None, description="员工姓名")


# ======================================================================
# 端点
# ======================================================================


@router.post("/oa/query", response_model=ApiResponse[dict])
async def query_oa(
    body: OAQueryRequest,
    api_key_info: dict = Depends(require_scope("connector:oa")),
) -> ApiResponse[dict]:
    """查询 OA 审批状态。

    根据单据编号查询 OA 系统中的审批进度与历史节点。
    当前为预留接口，返回占位结构供对接联调。
    """
    logger.info(
        "openapi.connector.oa_query",
        bill_no=body.bill_no,
        key_name=api_key_info.get("name"),
    )
    return ApiResponse(
        code=0,
        data={
            "bill_no": body.bill_no,
            "status": "pending",
            "current_node": "部门审批",
            "history": [],
            "message": "OA 连接器为预留接口，请对接实际 OA 系统后替换",
        },
        message="success",
    )


@router.post("/erp/query", response_model=ApiResponse[dict])
async def query_erp(
    body: ERPQueryRequest,
    api_key_info: dict = Depends(require_scope("connector:erp")),
) -> ApiResponse[dict]:
    """查询 ERP 数据。

    根据模块与查询条件从 ERP 系统获取业务数据。
    当前为预留接口，返回占位结构供对接联调。
    """
    logger.info(
        "openapi.connector.erp_query",
        module=body.module,
        key_name=api_key_info.get("name"),
    )
    return ApiResponse(
        code=0,
        data={
            "module": body.module,
            "filters": body.filters,
            "records": [],
            "total": 0,
            "message": "ERP 连接器为预留接口，请对接实际 ERP 系统后替换",
        },
        message="success",
    )


@router.post("/hr/employee", response_model=ApiResponse[dict])
async def query_employee(
    body: HREmployeeRequest,
    api_key_info: dict = Depends(require_scope("connector:hr")),
) -> ApiResponse[dict]:
    """查询员工信息。

    支持按工号、邮箱或姓名查询员工基本信息。
    当前为预留接口，返回占位结构供对接联调。
    """
    logger.info(
        "openapi.connector.hr_query",
        employee_id=body.employee_id,
        email=body.email,
        key_name=api_key_info.get("name"),
    )
    return ApiResponse(
        code=0,
        data={
            "found": False,
            "employee": None,
            "message": "HR 连接器为预留接口，请对接实际 HR/LDAP 系统后替换",
        },
        message="success",
    )
