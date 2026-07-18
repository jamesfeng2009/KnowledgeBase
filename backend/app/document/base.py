"""
文档解析器抽象层 — 单一职责：将原始文档解析为增强文本。

增强文本格式（统一返回 str）：
    - HTML 模式（默认）：纯文本 + HTML <table> 标签 + [图片描述: ...] / <img> 内联标注
    - Markdown 模式：纯文本 + Markdown 表格 + [图片描述: ...] / ![](url) 内联标注

分页支持：
    sections_to_text 支持 page_separator 参数，在页码变化时插入分隔标记，
    便于 chunker 按页边界分块和 RAG 检索引用页码。

遵循开闭原则：新增文档类型只需新增 Parser 并在 factory 注册，
无需修改既有解析器或 document_tasks 主流程。
遵循优雅降级：第三方库未安装时返回 content_text，不阻断主流程。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedSection:
    """解析后的文档片段 — 带类型标签，用于交错排列。

    Attributes:
        kind: 片段类型。
            - text: 纯文本
            - table: HTML <table> 标签
            - image_desc: "[图片描述: ...]" 内联标注（VLM 文本描述）
            - image_url: 图片已上传对象存储，content 可选保留 VLM 描述
        content: 片段文本内容。
            - text: 纯文本
            - table: HTML <table> 标签
            - image_desc: "[图片描述: ...]" 内联标注
            - image_url: VLM 描述（可为空字符串，仅保留 URL 时）
        page: 页码或 slide 编号（用于排序和分页分隔）。
        image_url: 图片在对象存储的 URL（仅 kind=image_url 时有效）。
    """

    kind: str
    content: str
    page: int = 0
    image_url: str | None = None


class DocumentParser(ABC):
    """文档解析器统一接口 — 所有格式解析器继承本抽象。"""

    @staticmethod
    def _bool(value: Any, default: bool = False) -> bool:
        """安全布尔转换 — 处理 MagicMock（测试场景）和非布尔值。

        MagicMock 在测试中是 truthy 但不是 bool 实例，
        此方法确保只有真正的 bool 值才被采纳，否则返回默认值。
        """
        if isinstance(value, bool):
            return value
        return default

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        """安全整数转换 — 处理 MagicMock（测试场景）和非整数值。

        MagicMock 无法与 int 比较，此方法确保只有真正的 int 才被采纳。
        """
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return default

    @abstractmethod
    async def parse(self, file_path: str) -> str:
        """解析文档，返回增强文本。

        Args:
            file_path: 文档文件路径。

        Returns:
            增强文本字符串（纯文本 + HTML 表格 + 图片描述/URL）。
            解析失败时返回空字符串，由调用方降级处理。
        """
        raise NotImplementedError

    @staticmethod
    def sections_to_text(
        sections: list[ParsedSection],
        output_format: str = "html",
        page_separator: str = "",
    ) -> str:
        """将 ParsedSection 列表合并为增强文本字符串。

        按页码排序，各片段用空行分隔。表格和图片保持原样，
        纯文本片段直接拼接。

        Args:
            sections: ParsedSection 列表。
            output_format: 输出格式 — "html"（默认）或 "markdown"。
                - html: 表格保持 HTML <table>，图片用 <img> 标签
                - markdown: 表格转为 Markdown 语法，图片用 ![](url) 语法
            page_separator: 页码分隔符，非空时在页码变化处插入。
                支持 ``{page}`` 占位符，替换为实际页码。
                例：``"\\n\\n---\\n<!-- page: {page} -->\\n"``
                默认空字符串（不分页标记，向后兼容）。

        Returns:
            合并后的增强文本字符串。
        """
        if not sections:
            return ""

        sorted_sections = sorted(sections, key=lambda s: s.page)
        parts: list[str] = []
        prev_page: int | None = None

        for sec in sorted_sections:
            text = DocumentParser._format_section(sec, output_format)
            if not text:
                continue

            # 分页分隔符 — 页码变化时插入
            if page_separator and prev_page is not None and sec.page != prev_page:
                sep = page_separator.replace("{page}", str(sec.page))
                parts.append(sep)

            parts.append(text)
            prev_page = sec.page

        return "\n\n".join(parts)

    @staticmethod
    def _format_section(sec: ParsedSection, output_format: str) -> str:
        """格式化单个 ParsedSection 为字符串。

        Args:
            sec: ParsedSection 实例。
            output_format: "html" 或 "markdown"。

        Returns:
            格式化后的文本。空字符串表示跳过。
        """
        content = sec.content.strip()

        if sec.kind == "text":
            return content

        if sec.kind == "table":
            if output_format == "markdown":
                return DocumentParser._html_table_to_markdown(content)
            return content

        if sec.kind == "image_desc":
            # 纯 VLM 文本描述，两种格式都一样
            return content

        if sec.kind == "image_url":
            url = sec.image_url or ""
            if not url:
                # 无 URL 时退化为描述
                return content
            if output_format == "markdown":
                img_md = f"![图片]({url})"
                # 同时有 VLM 描述时追加
                if content:
                    return f"{img_md}\n\n{content}"
                return img_md
            # HTML 模式
            img_html = f'<p><img src="{url}" alt="图片"/></p>'
            if content:
                return f"{img_html}\n<p>{content}</p>"
            return img_html

        return content

    @staticmethod
    def _html_table_to_markdown(html: str) -> str:
        """将 HTML <table> 转为 Markdown 表格语法。

        简易解析：提取 <tr> 行，<th>/<td> 单元格。
        第一行视为表头，自动插入分隔行 |---|---|。

        Args:
            html: HTML <table> 标签字符串。

        Returns:
            Markdown 表格字符串。解析失败时返回原 HTML。
        """
        if not html or "<table" not in html.lower():
            return html

        rows: list[list[str]] = []
        # 提取所有 <tr>...</tr>
        tr_pattern = re.compile(r"<tr>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
        cell_pattern = re.compile(
            r"<t[hd][^>]*>(.*?)</t[hd]>", re.DOTALL | re.IGNORECASE
        )

        for tr_match in tr_pattern.finditer(html):
            row_cells = []
            for cell_match in cell_pattern.finditer(tr_match.group(1)):
                cell_text = cell_match.group(1).strip()
                # 转义 Markdown 表格中的管道符
                cell_text = cell_text.replace("|", "\\|")
                row_cells.append(cell_text)
            if row_cells:
                rows.append(row_cells)

        if not rows:
            return html

        # 构造 Markdown 表格
        lines: list[str] = []
        # 表头
        header = rows[0]
        col_count = len(header)
        lines.append("| " + " | ".join(header) + " |")
        # 分隔行
        lines.append("| " + " | ".join(["---"] * col_count) + " |")
        # 数据行
        for row in rows[1:]:
            # 补齐列数（合并单元格时可能不对齐）
            while len(row) < col_count:
                row.append("")
            lines.append("| " + " | ".join(row[:col_count]) + " |")

        return "\n".join(lines)
