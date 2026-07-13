"""
分页工具 — 单一职责：分页参数、分页结果与分页查询执行。

遵循单一职责：
- PaginationParams 仅描述分页参数；
- PageResult 仅承载分页结果；
- paginate 仅执行分页查询（count + limit/offset）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")

# 单页上限，防止恶意大页请求拖垮数据库
_MAX_SIZE = 100


@dataclass
class PaginationParams:
    """分页参数 — page 从 1 开始，size 默认 20，上限 100。"""

    page: int = 1
    size: int = 20

    def __post_init__(self) -> None:
        if self.page < 1:
            self.page = 1
        if self.size < 1:
            self.size = 20
        if self.size > _MAX_SIZE:
            self.size = _MAX_SIZE

    @property
    def offset(self) -> int:
        """SQL OFFSET 值。"""
        return (self.page - 1) * self.size


@dataclass
class PageResult(Generic[T]):
    """泛型分页结果。"""

    items: list[T]
    total: int
    page: int
    size: int
    pages: int

    def to_dict(self) -> dict:
        """转为可序列化的 dict（items 原样返回，由上层 Pydantic 完成序列化）。"""
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "size": self.size,
            "pages": self.pages,
        }


async def paginate(
    query: Select,
    params: PaginationParams,
    session: AsyncSession,
) -> PageResult:
    """对 SQLAlchemy Select 语句执行分页查询。

    Args:
        query: ``select(...)`` 语句（可含 where / order_by 等过滤条件）。
        params: 分页参数。
        session: 异步数据库会话，用于执行查询。

    Returns:
        PageResult，包含当前页 items、总数 total 与总页数 pages。

    说明：
    - count 查询会去除 order_by 后用子查询计数，以提升性能；
    - items 查询应用 offset / limit，并使用 scalars() 取单实体；
    - 适用于无 GROUP BY 的常规分页查询。
    """
    # 总数：去掉排序后用子查询 count
    count_stmt = select(func.count()).select_from(query.order_by(None).subquery())
    total = await session.scalar(count_stmt)
    total = int(total) if total is not None else 0

    # 当前页数据
    paged_stmt = query.offset(params.offset).limit(params.size)
    result = await session.execute(paged_stmt)
    items = list(result.scalars().all())

    pages = math.ceil(total / params.size) if params.size else 0

    return PageResult(
        items=items,
        total=total,
        page=params.page,
        size=params.size,
        pages=pages,
    )
