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

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from app.api.openapi.deps import require_scope
from app.api.openapi.responses import not_implemented
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
    response: Response,
    api_key_info: dict = Depends(require_scope("connector:oa")),
) -> ApiResponse[dict]:
    """查询 OA 审批状态。

    .. warning::
       **能力尚未实现**，返回 HTTP 501 + ``code=501``（不是"查询成功但无数据"）。
       对接实际 OA 系统后应替换为真实查询结果。
    """
    logger.info(
        "openapi.connector.oa_query",
        bill_no=body.bill_no,
        key_name=api_key_info.get("name"),
    )
    return not_implemented(
        response,
        "OA 审批状态查询",
        hint="需先接入目标 OA 系统并实现审批进度拉取",
    )


@router.post("/erp/query", response_model=ApiResponse[dict])
async def query_erp(
    body: ERPQueryRequest,
    response: Response,
    api_key_info: dict = Depends(require_scope("connector:erp")),
) -> ApiResponse[dict]:
    """查询 ERP 数据。

    .. warning::
       **能力尚未实现**，返回 HTTP 501 + ``code=501``（不是"查询成功但无数据"）。
       对接实际 ERP 系统后应替换为真实查询结果。
    """
    logger.info(
        "openapi.connector.erp_query",
        module=body.module,
        key_name=api_key_info.get("name"),
    )
    return not_implemented(
        response,
        "ERP 数据查询",
        hint="需先接入目标 ERP 系统并实现模块化查询",
    )


@router.post("/hr/employee", response_model=ApiResponse[dict])
async def query_employee(
    body: HREmployeeRequest,
    response: Response,
    api_key_info: dict = Depends(require_scope("connector:hr")),
) -> ApiResponse[dict]:
    """查询员工信息。

    .. warning::
       **能力尚未实现**，返回 HTTP 501 + ``code=501``（不是"查无此人"）。
       对接 HR/LDAP 后应替换为真实查询结果。
    """
    logger.info(
        "openapi.connector.hr_query",
        employee_id=body.employee_id,
        email=body.email,
        key_name=api_key_info.get("name"),
    )
    return not_implemented(
        response,
        "员工信息查询",
        hint="需先接入 HR 系统或 LDAP 目录",
    )
