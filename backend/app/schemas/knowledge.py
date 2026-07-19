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
    # P1-2: AI 智能处理字段（模型已存在，Schema 补充暴露）
    summary: str | None = Field(default=None, description="AI 自动摘要")
    category: str | None = Field(
        default=None, description="AI 自动分类: 政策/SOP/技术文档/会议纪要/培训资料/产品文档/合同模板"
    )
    file_path: str | None = Field(default=None, description="原始文件路径")
    # P1-1: 解析元数据字段（迁移 a1b2c3d4e5f6 新增）
    parse_status: str | None = Field(
        default=None, description="解析状态: parsed/partial/failed/pending"
    )
    parse_warnings: list[str] | None = Field(
        default=None, description="解析警告列表"
    )
    page_count: int = Field(default=0, ge=0, description="页数/幻灯片数/工作表数")
    char_count: int = Field(default=0, ge=0, description="正文字符数")
    # 多租户隔离
    tenant_id: uuid.UUID | None = Field(default=None, description="租户 ID")
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


class DocumentSummaryResponse(BaseModel):
    """文档解析摘要响应 — 对齐竞品草稿摘要 JSON。

    P1 增强：上传/解析完成后返回结构化摘要，包含：
    - preview: 正文前 N 字符预览
    - structure: 文档结构标签列表（h1/h2/table/ul 等）
    - warnings: 解析过程中的警告信息
    - pages: 页数/幻灯片数/工作表数
    - char_count: 正文字符数
    - parse_status: 解析状态（parsed/partial/failed/pending）
    """

    model_config = ConfigDict(from_attributes=True)

    doc_id: uuid.UUID = Field(..., description="文档 ID")
    title: str = Field(..., description="文档标题")
    doc_type: str = Field(..., description="文档类型")
    status: str = Field(..., description="文档状态")
    preview: str = Field(default="", description="正文预览（前 500 字符）")
    structure: list[str] = Field(
        default_factory=list, description="文档结构标签（h1/h2/table/ul 等）"
    )
    warnings: list[str] = Field(
        default_factory=list, description="解析警告信息（小图过滤/降级/OCR 等）"
    )
    pages: int = Field(default=0, ge=0, description="页数/幻灯片数/工作表数")
    char_count: int = Field(default=0, ge=0, description="正文字符数")
    parse_status: str = Field(
        default="pending", description="解析状态: parsed/partial/failed/pending"
    )
    file_path: str | None = Field(default=None, description="原文件存储路径")
    created_at: datetime = Field(..., description="创建时间")
