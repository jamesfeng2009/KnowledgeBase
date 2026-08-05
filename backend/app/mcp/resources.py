"""
MCP Resources 原语 — Resource Metadata 定义与资源构建。

对齐 MCP 2026-07-28 规范 ``resources/list`` + ``resources/read``：
每个工具同时暴露为一个资源（URI: ``resource://skill/{tool_name}``），
携带结构化元数据，帮助 Agent 理解三件事：

1. **何时用**（when_to_use）— 正向适用场景；
2. **何时不用**（when_not_to_use）— 负向边界，防止误调用；
3. **如何解读输出**（output_interpretation）— 输出字段语义与注意事项。

元数据同时携带 ``version`` / ``review_status`` 支持治理：
- ``version``：语义化版本，变更元数据时递增；
- ``review_status``：``draft`` → ``reviewed`` → ``approved`` / ``deprecated``，
  未评审（draft）的资源在列表中标注，提示 Agent 谨慎依赖其边界描述。

遵循单一职责：本模块只定义资源数据模型与元数据解析，不涉及传输逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 资源 URI 前缀 — 每个技能（工具）一个资源
RESOURCE_URI_SCHEME = "resource://skill/"

#: review_status 合法取值
REVIEW_STATUSES: tuple[str, ...] = ("draft", "reviewed", "approved", "deprecated")


@dataclass
class ResourceMetadata:
    """资源元数据 — 描述一个 MCP 资源的使用边界与输出解读方式。

    Attributes:
        domain: 业务域（通常取工具的 category，如 search / document / workflow）。
        tags: 标签列表，用于检索与分组。
        when_to_use: 正向适用场景描述。
        when_not_to_use: 负向边界描述（哪些场景不应使用）。
        output_interpretation: 输出解读说明（字段语义、截断标记、错误结构等）。
        version: 元数据语义化版本。
        review_status: 评审状态（draft / reviewed / approved / deprecated）。
    """

    domain: str = "general"
    tags: list[str] = field(default_factory=list)
    when_to_use: str = ""
    when_not_to_use: str = ""
    output_interpretation: str = ""
    version: str = "1.0.0"
    review_status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "tags": list(self.tags),
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "output_interpretation": self.output_interpretation,
            "version": self.version,
            "review_status": self.review_status,
        }


@dataclass
class Resource:
    """MCP 资源 — URI + 名称 + 描述 + 元数据 + 内容。

    Attributes:
        uri: 资源唯一标识（``resource://skill/{tool_name}``）。
        name: 资源名称（与工具同名）。
        description: 资源简述。
        mime_type: 内容类型（资源内容统一为 JSON）。
        metadata: 结构化资源元数据。
        content: 资源内容（JSON 字符串，resources/read 时返回）。
    """

    uri: str
    name: str
    description: str
    mime_type: str = "application/json"
    metadata: ResourceMetadata = field(default_factory=ResourceMetadata)
    content: str = ""

    def to_list_dict(self) -> dict[str, Any]:
        """序列化为 resources/list 响应项（不含 content，避免列表过大）。"""
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
            "metadata": self.metadata.to_dict(),
        }

    def to_read_dict(self) -> dict[str, Any]:
        """序列化为 resources/read 响应项（含 content）。"""
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
            "metadata": self.metadata.to_dict(),
            "text": self.content,
        }


def split_usage_boundaries(description: str) -> tuple[str, str]:
    """从工具描述中提取「适用场景 / 不适用于」两段文本。

    项目内工具描述约定包含 ``适用场景：...`` 与 ``不适用于：...`` 两个段落，
    本函数将其拆分为 (when_to_use, when_not_to_use)；未匹配到时返回空串，
    由调用方决定兜底（通常回退为完整描述）。

    Args:
        description: 工具描述原文。

    Returns:
        ``(when_to_use, when_not_to_use)`` 二元组。
    """
    when_to_use = ""
    when_not_to_use = ""
    if not description:
        return when_to_use, when_not_to_use

    text = description.strip()
    pos_marker = "适用场景："
    neg_marker = "不适用于："

    neg_idx = text.find(neg_marker)
    if neg_idx >= 0:
        when_not_to_use = text[neg_idx + len(neg_marker):].strip().rstrip("。")
        text = text[:neg_idx]

    pos_idx = text.find(pos_marker)
    if pos_idx >= 0:
        when_to_use = text[pos_idx + len(pos_marker):].strip().rstrip("。")

    return when_to_use, when_not_to_use


def make_resource_uri(tool_name: str) -> str:
    """构造工具对应的资源 URI。"""
    return f"{RESOURCE_URI_SCHEME}{tool_name}"


def parse_resource_uri(uri: str) -> str | None:
    """从资源 URI 解析工具名；非法 URI 返回 None。"""
    if not isinstance(uri, str) or not uri.startswith(RESOURCE_URI_SCHEME):
        return None
    name = uri[len(RESOURCE_URI_SCHEME):].strip()
    return name or None
