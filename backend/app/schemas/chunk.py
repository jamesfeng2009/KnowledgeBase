"""
分块 Schema — P0-2 分块编辑与版本历史的请求/响应数据模型。

遵循单一职责：仅定义请求体结构，响应以 dict 形式由路由直接构造。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChunkEdit(BaseModel):
    """编辑分块请求体。"""

    model_config = ConfigDict(from_attributes=True)

    content: str = Field(..., min_length=1, description="新正文内容")
    summary: str | None = Field(default=None, max_length=255, description="编辑摘要")
