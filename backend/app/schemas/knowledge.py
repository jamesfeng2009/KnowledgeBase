"""
知识库与文档 Schema — 单一职责：知识库、文档及文档版本的请求/响应数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，不包含检索、权限过滤等业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KbVisibility(str, Enum):
    """知识库可见性。"""

    public = "public"
    private = "private"
    dept = "dept"


class DocType(str, Enum):
    """文档类型。"""

    md = "md"
    html = "html"
    docx = "docx"
    pdf = "pdf"


class DocStatus(str, Enum):
    """文档状态。"""

    draft = "draft"
    published = "published"
    archived = "archived"


class Classification(str, Enum):
    """文档密级。"""

    public = "public"
    internal = "internal"
    confidential = "confidential"
    secret = "secret"


class KbCreate(BaseModel):
    """知识库创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., min_length=1, max_length=255, description="知识库名称")
    description: str | None = Field(default=None, description="描述")
    visibility: KbVisibility = Field(default=KbVisibility.private, description="可见性")
    dept_id: uuid.UUID | None = Field(default=None, description="部门 ID")
    tags: list[str] | None = Field(default=None, description="标签列表")


class KbUpdate(BaseModel):
    """知识库更新请求 — 所有字段可选，用于部分更新。"""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    visibility: KbVisibility | None = None
    dept_id: uuid.UUID | None = None
    tags: list[str] | None = None


class KbResponse(BaseModel):
    """知识库响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="知识库 ID")
    name: str = Field(..., description="知识库名称")
    description: str | None = Field(default=None, description="描述")
    visibility: KbVisibility = Field(..., description="可见性")
    owner_id: uuid.UUID = Field(..., description="所有者 ID")
    dept_id: uuid.UUID | None = Field(default=None, description="部门 ID")
    tags: list[str] | None = Field(default=None, description="标签列表")
    created_at: datetime = Field(..., description="创建时间")


class DocCreate(BaseModel):
    """文档创建请求 — 不含 id 与 created_at。"""

    model_config = ConfigDict(from_attributes=True)

    kb_id: uuid.UUID = Field(..., description="所属知识库 ID")
    title: str = Field(..., min_length=1, max_length=500, description="标题")
    content_html: str | None = Field(default=None, description="HTML 内容")
    content_json: dict[str, Any] | None = Field(default=None, description="Tiptap JSON")
    content_text: str | None = Field(default=None, description="纯文本（检索用）")
    doc_type: DocType = Field(default=DocType.md, description="文档类型")
    classification: Classification = Field(
        default=Classification.internal, description="密级"
    )


class DocUpdate(BaseModel):
    """文档更新请求 — 所有字段可选，用于部分更新。"""

    model_config = ConfigDict(from_attributes=True)

    title: str | None = Field(default=None, min_length=1, max_length=500)
    content_html: str | None = None
    content_json: dict[str, Any] | None = None
    content_text: str | None = None
    doc_type: DocType | None = None
    status: DocStatus | None = None
    classification: Classification | None = None


class DocResponse(BaseModel):
    """文档响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="文档 ID")
    kb_id: uuid.UUID = Field(..., description="所属知识库 ID")
    title: str = Field(..., description="标题")
    content_html: str | None = Field(default=None, description="HTML 内容")
    content_json: dict[str, Any] | None = Field(default=None, description="Tiptap JSON")
    content_text: str | None = Field(default=None, description="纯文本（检索用）")
    doc_type: DocType = Field(..., description="文档类型")
    status: DocStatus = Field(..., description="状态")
    owner_id: uuid.UUID = Field(..., description="所有者 ID")
    dept_id: uuid.UUID | None = Field(default=None, description="部门 ID")
    classification: Classification = Field(..., description="密级")
    view_count: int = Field(default=0, ge=0, description="浏览次数")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class DocVersionResponse(BaseModel):
    """文档版本响应 — 协同编辑历史快照。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="版本 ID")
    doc_id: uuid.UUID = Field(..., description="文档 ID")
    content_html: str | None = Field(default=None, description="HTML 快照")
    content_json: dict[str, Any] | None = Field(default=None, description="JSON 快照")
    author_id: uuid.UUID = Field(..., description="操作者 ID")
    summary: str | None = Field(default=None, max_length=255, description="版本摘要")
    created_at: datetime = Field(..., description="创建时间")
