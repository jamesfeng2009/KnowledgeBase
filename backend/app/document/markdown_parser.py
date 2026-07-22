"""Markdown 文档解析器 — 将 .md 文件解析为增强文本。

支持 Obsidian、Notion 导出、飞书/Confluence 导出的 Markdown 文件。
Markdown 本身就是结构化格式，解析器只需读取文件内容并返回，
chunker 的 _is_markdown + _split_markdown 会按 # / ## / ### 标题分块。

设计决策：
- Markdown 不做格式转换，直接返回原始文本（chunker 原生支持 Markdown 分块）
- 支持 frontmatter 解析（YAML 元数据头），提取标题/日期/标签
- 支持图片链接保持 ![](url) 格式（与 base.py Markdown 模式一致）
"""
from __future__ import annotations

import re
from pathlib import Path

from app.document.base import DocumentParser
from app.utils.logger import get_logger

log = get_logger(__name__)

# frontmatter 正则 — 匹配 YAML 元数据头（--- ... ---）
_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL
)

# frontmatter 中的 title 字段
_TITLE_PATTERN = re.compile(r"^title:\s*(.+)$", re.MULTILINE)


class MarkdownParser(DocumentParser):
    """Markdown 文档解析器 — 读取 .md 文件，返回增强文本。

    Markdown 文件天然含标题结构（# / ## / ###），chunker 的
    ``_is_markdown`` + ``_split_markdown`` 可直接按标题分块，
    无需额外处理。

    支持：
    - Obsidian .md 文件（含 [[wiki links]]、![](image) 嵌入）
    - Notion 导出的 Markdown
    - 飞书/Confluence 导出的 Markdown
    - 标准 CommonMark Markdown
    - YAML frontmatter（提取 title 元数据）
    """

    @staticmethod
    def is_available() -> bool:
        """Markdown 解析器不依赖第三方库，始终可用。"""
        return True

    @staticmethod
    def is_supported(doc_type: str) -> bool:
        """检查是否支持该文档类型。"""
        return doc_type.lower() in ("md", "markdown")

    async def parse(self, file_path: str) -> str:
        """解析 Markdown 文件，返回增强文本。

        Args:
            file_path: Markdown 文件路径。

        Returns:
            增强文本字符串（原始 Markdown，含标题/表格/图片链接）。
        """
        try:
            path = Path(file_path)
            if not path.exists():
                log.warning("markdown_parser.file_not_found", path=file_path)
                return ""

            content = path.read_text(encoding="utf-8")
            return self._parse_content(content)
        except Exception as exc:
            log.warning("markdown_parser.parse_error", error=str(exc))
            return ""

    def parse_from_content(self, content: str) -> str:
        """从字符串内容解析（适配器拉取的 Markdown 无需落盘）。

        Args:
            content: Markdown 文本内容。

        Returns:
            增强文本字符串。
        """
        return self._parse_content(content)

    def _parse_content(self, content: str) -> str:
        """解析 Markdown 内容 — 提取 frontmatter 并保持正文结构。

        Args:
            content: 原始 Markdown 文本。

        Returns:
            增强文本：可选 frontmatter title 作为 h1 + 正文。
        """
        if not content or not content.strip():
            return ""

        # 提取 frontmatter
        frontmatter_match = _FRONTMATTER_PATTERN.match(content)
        title: str | None = None
        body = content

        if frontmatter_match:
            frontmatter = frontmatter_match.group(1)
            title_match = _TITLE_PATTERN.search(frontmatter)
            if title_match:
                title = title_match.group(1).strip().strip('"').strip("'")

            # 去除 frontmatter，保留正文
            body = content[frontmatter_match.end():]

        # 如果 frontmatter 有 title 且正文不以 h1 标题（"# "）开头，
        # 补一个 h1 标题让 chunker 能结构化分块
        # 注意：不能用 startswith("#")，因为 "## "（h2）也会匹配
        if title and not body.lstrip().startswith("# "):
            body = f"# {title}\n\n{body}"

        return body.strip()
