"""
文件夹 Schema — P0-3 文件夹树的请求/响应数据模型。

遵循单一职责：仅定义请求体结构，响应以 dict 形式由路由直接构造
（树节点为递归结构，不引入递归 Pydantic 模型）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FolderCreate(BaseModel):
    """创建文件夹请求体。"""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., min_length=1, max_length=255, description="文件夹名（单段，不含 '/'）")
    parent_path: str | None = Field(default=None, max_length=1000, description="父路径（None=根级）")
    sort_order: int | None = Field(default=None, ge=0, description="同级排序（可选）")


class FolderRename(BaseModel):
    """重命名文件夹请求体。"""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., min_length=1, max_length=255, description="新名称（单段，不含 '/'）")


class DocumentMove(BaseModel):
    """移动文档请求体。"""

    model_config = ConfigDict(from_attributes=True)

    path: str | None = Field(default=None, max_length=1000, description="目标文件夹路径（None=根级）")
    sort_order: int | None = Field(default=None, ge=0, description="同级排序（可选）")
