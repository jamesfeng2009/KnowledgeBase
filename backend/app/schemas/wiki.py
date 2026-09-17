"""
Wiki Schema — P0-1 Wiki 页面的请求/响应数据模型。

遵循单一职责：仅定义请求体结构，响应以 dict 形式由路由直接构造。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WikiPageUpdate(BaseModel):
    """编辑 Wiki 页面请求体。"""

    model_config = ConfigDict(from_attributes=True)

    title: str = Field(..., min_length=1, max_length=255, description="页面标题")
    content_md: str = Field(..., min_length=1, description="Markdown 正文")
    summary: str | None = Field(default=None, max_length=255, description="编辑摘要")
