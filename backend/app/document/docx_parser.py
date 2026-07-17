"""
DOCX 文档解析器 — 单一职责：将 DOCX 解析为增强文本（文本 + HTML 表格 + 图片描述）。

使用 python-docx 提取段落文本、表格和内嵌图片：
    - 文本：doc.paragraphs（按文档顺序）
    - 表格：doc.tables → HTML <table>
    - 图片：inline_shapes / related_parts → VLM 描述

通过遍历 doc.element.body 保持原始文档顺序（段落与表格交错出现），
而非先提取所有段落后提取所有表格。

遵循优雅降级：
    - python-docx 未安装 → 返回空字符串，调用方降级；
    - VLM 不可用 → 跳过图片描述，只保留文本和表格；
    - 配置开关可单独关闭表格提取或图片提取。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.document.base import DocumentParser, ParsedSection
from app.utils.logger import get_logger

log = get_logger(__name__)

# VLM 并发控制
_VLM_SEMAPHORE_LIMIT: int = 3
_IMAGE_PROMPT: str = (
    "请用一句话描述这张图片的内容，"
    "重点关注图表、数据、文字和关键信息，便于后续检索。"
)


class DOCXParser(DocumentParser):
    """DOCX 解析器 — python-docx 文本 + 表格 + 内嵌图片 VLM。"""

    async def parse(self, file_path: str) -> str:
        """解析 DOCX 文档，返回增强文本。

        Args:
            file_path: DOCX 文件路径。

        Returns:
            增强文本（纯文本 + HTML 表格 + 图片描述）。
            python-docx 未安装或解析失败时返回空字符串。
        """
        try:
            from docx import Document as DocxDocument
        except ImportError:
            log.warning("docx.parser_skipped", reason="python-docx not installed")
            return ""

        try:
            docx_doc = DocxDocument(file_path)
        except Exception as exc:
            log.warning("docx.open_failed", file_path=file_path, error=str(exc))
            return ""

        settings = get_settings()
        table_enabled = getattr(settings, "DOCX_TABLE_EXTRACTION_ENABLED", True)
        image_enabled = getattr(settings, "DOCX_IMAGE_EXTRACTION_ENABLED", True)
        max_images = getattr(settings, "DOCX_IMAGE_MAX_PER_DOC", 50)

        sections: list[ParsedSection] = []
        image_count = 0

        # 遍历文档 body 元素，保持段落与表格的原始顺序
        seq = 0  # 序号用于排序，保持文档原序
        for element in docx_doc.element.body:
            tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag

            if tag == "p":
                # 段落
                text = self._extract_paragraph_text(element)
                if text and text.strip():
                    sections.append(
                        ParsedSection(kind="text", content=text.strip(), page=seq)
                    )
                    seq += 1
            elif tag == "tbl" and table_enabled:
                # 表格
                html = self._extract_table_html(element)
                if html:
                    sections.append(
                        ParsedSection(kind="table", content=html, page=seq)
                    )
                    seq += 1

        # 提取图片（独立于 body 遍历，因为图片嵌在段落中）
        if image_enabled and image_count < max_images:
            img_sections, img_count = await self._extract_images(
                docx_doc, max_images - image_count
            )
            sections.extend(img_sections)
            image_count += img_count

        log.info(
            "docx.parsed",
            file_path=file_path,
            sections=len(sections),
            tables=sum(1 for s in sections if s.kind == "table"),
            images=sum(1 for s in sections if s.kind == "image_desc"),
        )
        return self.sections_to_text(sections)

    @staticmethod
    def _extract_paragraph_text(element: Any) -> str:
        """从 <w:p> XML 元素提取段落文本。

        使用 python-docx 的 Paragraph 包装器获取格式化文本。
        """
        try:
            from docx.text.paragraph import Paragraph

            para = Paragraph(element, None)
            return para.text or ""
        except Exception:
            return ""

    @staticmethod
    def _extract_table_html(element: Any) -> str:
        """从 <w:tbl> XML 元素提取表格并转为 HTML。

        使用 python-docx 的 Table 包装器获取行列数据。
        """
        try:
            from docx.table import Table

            table = Table(element, None)
            rows: list[list[str | None]] = []
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                rows.append(cells)
            return DOCXParser._rows_to_html(rows)
        except Exception as exc:
            log.debug("docx.table_extract_failed", error=str(exc))
            return ""

    @staticmethod
    def _rows_to_html(rows: list[list[str | None]]) -> str:
        """将二维数据转为 HTML <table> 标签。

        第一行视为表头（<th>），其余为数据行（<td>）。
        """
        if not rows:
            return ""

        lines: list[str] = ["<table>"]

        for i, row in enumerate(rows):
            lines.append("<tr>")
            tag = "th" if i == 0 else "td"
            for cell in row:
                cell_text = (cell or "").strip()
                cell_text = (
                    cell_text.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                lines.append(f"<{tag}>{cell_text}</{tag}>")
            lines.append("</tr>")

        lines.append("</table>")
        return "\n".join(lines)

    async def _extract_images(
        self,
        docx_doc: Any,
        remaining: int,
    ) -> tuple[list[ParsedSection], int]:
        """提取 DOCX 内嵌图片，调用 VLM 生成描述。

        通过 doc.part.related_parts 遍历所有图片关系，
        提取 image part 的 blob 作为图片二进制数据。

        Args:
            docx_doc: python-docx Document 对象。
            remaining: 本文档剩余可提取图片数。

        Returns:
            (图片描述 ParsedSection 列表, 实际提取的图片数)
        """
        try:
            # 收集所有图片 part
            image_parts: list[tuple[bytes, str]] = []
            seen_rids: set[str] = set()

            for rid, rel in docx_doc.part.rels.items():
                if "image" not in rel.reltype.lower():
                    continue
                if rid in seen_rids:
                    continue
                seen_rids.add(rid)

                try:
                    blob = rel.target_part.blob
                    content_type = rel.target_part.content_type or "image/png"
                    if blob and len(blob) > 0:
                        image_parts.append((blob, content_type))
                except Exception as exc:
                    log.debug("docx.image_part_failed", rid=rid, error=str(exc))

            if not image_parts:
                return [], 0

            image_parts = image_parts[:remaining]

            semaphore = asyncio.Semaphore(_VLM_SEMAPHORE_LIMIT)

            async def describe_image(
                img_bytes: bytes, content_type: str
            ) -> ParsedSection | None:
                try:
                    async with semaphore:
                        desc = await self._vlm_describe(img_bytes, content_type)
                    if not desc or not desc.strip():
                        return None
                    return ParsedSection(
                        kind="image_desc",
                        content=f"[图片描述: {desc.strip()}]",
                        page=9999,  # 图片排在文本之后
                    )
                except Exception as exc:
                    log.warning("docx.image_vlm_error", error=str(exc))
                    return None

            tasks = [describe_image(blob, ct) for blob, ct in image_parts]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            sections: list[ParsedSection] = []
            count = 0
            for result in results:
                if isinstance(result, ParsedSection):
                    sections.append(result)
                    count += 1
                elif isinstance(result, Exception):
                    log.warning("docx.image_gather_error", error=str(result))

            return sections, count

        except Exception as exc:
            log.warning("docx.image_extract_error", error=str(exc))
            return [], 0

    async def _vlm_describe(self, image: bytes, mime_type: str) -> str:
        """调用 VLM 理解图片内容 — 延迟导入，不可用时返回空字符串。"""
        try:
            from app.vlm.provider import get_vision_provider

            vlm = get_vision_provider()
            return await vlm.understand(
                image=image,
                prompt=_IMAGE_PROMPT,
                mime_type=mime_type,
            )
        except ImportError:
            log.warning("docx.vlm_not_available")
            return ""
        except Exception as exc:
            log.warning("docx.vlm_error", error=str(exc))
            return ""
