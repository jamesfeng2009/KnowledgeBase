"""
通用 Schema — 单一职责：定义统一 API 响应与分页数据结构。

遵循单一职责：仅定义通用包装结构，不包含业务逻辑。
遵循开闭原则：新增业务领域 Schema 无需修改本模块。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一 API 响应结构 — ``{ code, data, message }``。

    - code 为 0 表示成功，非 0 表示业务错误码；
    - data 为泛型载荷，失败时可空；
    - message 为人类可读提示信息。
    """

    model_config = ConfigDict(from_attributes=True)

    code: int = Field(default=0, ge=0, description="业务状态码，0 表示成功")
    data: T | None = Field(default=None, description="响应数据载荷")
    message: str = Field(default="", description="提示信息")


class PaginationParams(BaseModel):
    """分页查询参数 — page 从 1 开始，size 默认 20，上限 100。"""

    model_config = ConfigDict(from_attributes=True)

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    size: int = Field(default=20, ge=1, le=100, description="每页数量，范围 1-100")


class PageResponse(BaseModel, Generic[T]):
    """泛型分页响应结构。"""

    model_config = ConfigDict(from_attributes=True)

    items: list[T] = Field(default_factory=list, description="当前页数据")
    total: int = Field(default=0, ge=0, description="总记录数")
    page: int = Field(default=1, ge=1, description="当前页码")
    size: int = Field(default=20, ge=1, description="每页数量")
    pages: int = Field(default=0, ge=0, description="总页数")
