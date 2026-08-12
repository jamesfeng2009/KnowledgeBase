"""
文档来源适配器抽象基类 — 定义统一接口：fetch / list_documents / test_connection。

所有平台适配器（Confluence/Obsidian/飞书/Notion）继承此基类，
实现各自的 fetch 方法返回统一格式 FetchedDocument。

设计决策：
- 适配器只负责"把外部平台文档拉取为统一中间格式"，不做解析/分块/向量化
- 统一中间格式为 content（str），format 为 "html" 或 "markdown"
  - HTML 路径：Confluence/飞书导出 → WikiHtmlCleaner 清洗 → chunker._split_html
  - Markdown 路径：Obsidian/Notion → MarkdownParser → chunker._split_markdown
- 凭证通过 credentials dict 传入（API token/OAuth/access_token），
  适配器不负责凭证存储，由调用方从 Connector 凭证管理获取
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FetchedDocument:
    """从外部平台拉取的文档 — 统一中间格式。

    适配器 fetch 方法返回此格式，后续由 document_tasks 流水线消费：
        FetchedDocument → 解析器（HTML 清洗 / Markdown 解析）→ chunker → 向量化

    Attributes:
        source: 来源平台标识（confluence / obsidian / feishu / notion）。
        title: 文档标题。
        content: 文档内容（HTML 或 Markdown 字符串）。
        format: 内容格式 — ``"html"`` 或 ``"markdown"``。
        source_url: 原始文档 URL（点击跳转到外部平台）。
        doc_id: 外部平台的文档 ID（Confluence pageId / Notion pageId 等）。
        metadata: 附加元数据（作者/更新时间/空间/标签等）。
    """

    source: str
    title: str
    content: str
    format: str  # "html" | "markdown"
    source_url: str = ""
    doc_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceDocumentInfo:
    """外部平台文档列表项 — 用于 list_documents 返回。

    Attributes:
        doc_id: 外部文档 ID。
        title: 文档标题。
        url: 文档 URL。
        updated_at: 最后更新时间（ISO 8601 字符串）。
        author: 最后修改者。
        metadata: 附加元数据。
    """

    doc_id: str
    title: str
    url: str = ""
    updated_at: str = ""
    author: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RevisionInfo:
    """外部文档版本指纹 — 用于回源前的轻量变更检测（P0 阶段 A）。

    各适配器 get_revision 返回此对象，调用方对比 fingerprint 与
    Document.source_revision 判断是否需要拉取全文：
        - 一致 → 信任本地，不调 fetch（节省 API + 网络成本）
        - 不一致 → 进入阶段 B（fetch 全文 + hash 对比）

    Attributes:
        fingerprint: 版本指纹字符串。各平台语义不同：
            - 飞书: revision_id（数字字符串，如 ``"42"``）
            - Confluence: version.number（数字字符串，如 ``"5"``）
            - Notion: last_edited_time（ISO 8601，如 ``"2026-08-12T10:00:00.000Z"``）
            - Obsidian: st_mtime_ns（纳秒时间戳字符串，如 ``"1786528000000000000"``）
        last_modified: 最后修改时间（可选，用于 P3 prompt 时效声明）。
            None 表示平台未返回。
    """

    fingerprint: str
    last_modified: Any = None  # datetime | str | None


class DocumentSourceAdapter(ABC):
    """文档来源适配器抽象基类 — 所有平台适配器实现此接口。

    子类需实现：
        - fetch(doc_url_or_id, credentials) → FetchedDocument
        - list_documents(space_or_root, credentials) → list[SourceDocumentInfo]
        - test_connection(credentials) → bool

    属性：
        - adapter_id: 适配器标识（如 ``"confluence"`` / ``"obsidian"``）
        - display_name: 显示名称（如 ``"Confluence"`` / ``"Obsidian"``）
        - supported_formats: 该适配器输出的内容格式列表
    """

    adapter_id: str = "base"
    display_name: str = "基础适配器"
    supported_formats: tuple[str, ...] = ("html",)

    @abstractmethod
    async def fetch(
        self,
        doc_url_or_id: str,
        credentials: dict[str, Any],
    ) -> FetchedDocument:
        """从外部平台拉取单个文档。

        Args:
            doc_url_or_id: 文档 URL 或 ID（Confluence pageId / Notion pageId /
                Obsidian 文件路径等）。
            credentials: 平台凭证（API token / OAuth access_token 等）。

        Returns:
            统一格式的 FetchedDocument。

        Raises:
            AdapterError: 拉取失败（网络错误/权限不足/文档不存在）。
        """
        ...

    @abstractmethod
    async def list_documents(
        self,
        space_or_root: str,
        credentials: dict[str, Any],
    ) -> list[SourceDocumentInfo]:
        """列出外部平台某空间/目录下的文档。

        Args:
            space_or_root: 空间 Key（Confluence space key）或根目录路径
                （Obsidian vault 路径 / Notion database ID）。
            credentials: 平台凭证。

        Returns:
            文档信息列表。

        Raises:
            AdapterError: 列举失败。
        """
        ...

    @abstractmethod
    async def test_connection(self, credentials: dict[str, Any]) -> bool:
        """测试平台连接是否正常。

        Args:
            credentials: 平台凭证。

        Returns:
            连接是否成功。
        """
        ...

    async def get_revision(
        self,
        doc_url_or_id: str,
        credentials: dict[str, Any],
    ) -> RevisionInfo | None:
        """轻量查询文档版本指纹 — 不拉取内容，仅查元信息（P0 阶段 A）。

        用于回源校验前的快速变更检测：调用方对比返回的 fingerprint 与
        Document.source_revision 判断是否需要 fetch 全文。

        默认实现返回 None，表示该适配器不支持轻量探测；
        调用方应降级为直接 fetch 全文（仍能完成同步，仅多一次拉取成本）。

        Args:
            doc_url_or_id: 文档 URL 或 ID。
            credentials: 平台凭证。

        Returns:
            RevisionInfo 或 None（不支持时）。

        Raises:
            AdapterError: 探测失败（网络错误/权限不足）— 调用方应降级信任本地。
        """
        return None


class AdapterError(Exception):
    """适配器异常 — 拉取/列举/测试连接失败时抛出。"""

    def __init__(self, adapter_id: str, message: str, status_code: int = 0) -> None:
        self.adapter_id = adapter_id
        self.status_code = status_code
        super().__init__(f"[{adapter_id}] {message}")
