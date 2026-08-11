"""
搜索 Schema — 单一职责：搜索请求与响应的数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，
不包含检索引擎调用、权限过滤等业务逻辑。
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchType(str, Enum):
    """搜索类型。"""

    fulltext = "fulltext"
    vector = "vector"
    hybrid = "hybrid"


class SearchRequest(BaseModel):
    """搜索请求 — 支持全文/向量/混合检索。"""

    model_config = ConfigDict(from_attributes=True)

    query: str = Field(..., min_length=1, max_length=1000, description="查询词")
    kb_ids: list[uuid.UUID] | None = Field(
        default=None, description="知识库 ID 列表（为空表示搜索全部可访问知识库）"
    )
    search_type: SearchType = Field(
        default=SearchType.hybrid, description="搜索类型: fulltext/vector/hybrid"
    )
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量")
    # P0 wiki 层级：检索层级过滤 — 透传到 retriever.search，由
    # filter_builder 转为后端 filter 子句。可选 key：
    #   series_id / path_prefix / parent_id / depth / version_of
    filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "层级过滤条件，可选 key: "
            "series_id(系列精确匹配) / path_prefix(路径前缀匹配子树) / "
            "parent_id(直系父文档) / depth(层级深度,根=0) / version_of(版本族主文档)"
        ),
    )


class SearchResult(BaseModel):
    """单条搜索结果。"""

    model_config = ConfigDict(from_attributes=True)

    doc_id: uuid.UUID = Field(..., description="文档 ID")
    title: str = Field(..., description="文档标题")
    snippet: str = Field(default="", description="内容摘要片段")
    score: float = Field(..., ge=0.0, le=1.0, description="相关性分数（0-1）")
    source: str | None = Field(default=None, description="来源标识")
    kb_name: str | None = Field(default=None, description="所属知识库名称")
    highlights: list[str] | None = Field(
        default=None, description="高亮匹配片段列表"
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description="附加元数据"
    )


class SearchResponse(BaseModel):
    """搜索响应 — 包含结果列表与总数。"""

    model_config = ConfigDict(from_attributes=True)

    results: list[SearchResult] = Field(
        default_factory=list, description="搜索结果列表"
    )
    total: int = Field(default=0, ge=0, description="总匹配数")
    query: str = Field(..., description="原始查询词")


class SearchSuggestion(BaseModel):
    """搜索建议（自动补全）。"""

    model_config = ConfigDict(from_attributes=True)

    text: str = Field(..., description="建议文本")
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="建议分数")


class ReindexRequest(BaseModel):
    """重建索引请求。"""

    model_config = ConfigDict(from_attributes=True)

    kb_ids: list[uuid.UUID] | None = Field(
        default=None, description="指定知识库 ID（为空表示全部重建）"
    )
    force: bool = Field(default=False, description="是否强制全量重建（忽略增量）")


class ReindexResponse(BaseModel):
    """重建索引响应。"""

    model_config = ConfigDict(from_attributes=True)

    task_id: str = Field(..., description="异步任务 ID")
    status: str = Field(default="queued", description="任务状态")
    message: str = Field(default="索引重建任务已提交", description="提示信息")
