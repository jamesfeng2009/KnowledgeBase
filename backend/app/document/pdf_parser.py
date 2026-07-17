"""
PDF 文档解析器 — 单一职责：将 PDF 解析为增强文本（文本 + HTML 表格 + 图片描述）。

两阶段能力：
    Phase 1: 表格提取 — pymupdf page.find_tables() → HTML <table> 标签；
    Phase 2: 图片提取 — pymupdf get_images() → VLM.understand() → [图片描述: ...]。

遵循优雅降级：
    - pymupdf 未安装 / find_tables 不可用 → 退化为 page.get_text() 纯文本；
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

# VLM 并发控制 — 防止大量图片同时调用 VLM 打满服务
_VLM_SEMAPHORE_LIMIT: int = 3
# 图片 VLM 理解 prompt
_IMAGE_PROMPT: str = (
    "请用一句话描述这张图片的内容，"
    "重点关注图表、数据、文字和关键信息，便于后续检索。"
)


class PDFParser(DocumentParser):
    """PDF 解析器 — 文本 + 表格（Phase 1）+ 图片 VLM 描述（Phase 2）。

    使用 pymupdf (fitz) 完成所有提取，无额外依赖。
    """

    async def parse(self, file_path: str) -> str:
        """解析 PDF 文档，返回增强文本。

        Args:
            file_path: PDF 文件路径。

        Returns:
            增强文本（纯文本 + HTML 表格 + [图片描述]）。
            pymupdf 未安装或解析失败时返回空字符串。
        """
        try:
            import fitz  # pymupdf  # noqa: F401
        except ImportError:
            log.warning("pdf.parser_skipped", reason="pymupdf not installed")
            return ""

        try:
            import fitz

            doc = fitz.open(file_path)
        except Exception as exc:
            log.warning("pdf.open_failed", file_path=file_path, error=str(exc))
            return ""

        settings = get_settings()
        table_enabled = getattr(settings, "PDF_TABLE_EXTRACTION_ENABLED", True)
        image_enabled = getattr(settings, "PDF_IMAGE_EXTRACTION_ENABLED", True)
        max_images = getattr(settings, "PDF_IMAGE_MAX_PER_DOC", 50)

        sections: list[ParsedSection] = []
        image_count = 0

        try:
            for page_num in range(len(doc)):
                page = doc[page_num]

                # 1. 提取文本
                text = page.get_text().strip()
                if text:
                    sections.append(
                        ParsedSection(kind="text", content=text, page=page_num)
                    )

                # 2. 提取表格（Phase 1）
                if table_enabled:
                    table_sections = self._extract_tables(page, page_num)
                    sections.extend(table_sections)

                # 3. 提取图片并 VLM 描述（Phase 2）
                if image_enabled and image_count < max_images:
                    img_sections, img_count = await self._extract_images(
                        doc, page, page_num, max_images - image_count
                    )
                    sections.extend(img_sections)
                    image_count += img_count

        finally:
            doc.close()

        log.info(
            "pdf.parsed",
            file_path=file_path,
            pages=len(doc) if doc else 0,
            sections=len(sections),
            tables=sum(1 for s in sections if s.kind == "table"),
            images=sum(1 for s in sections if s.kind == "image_desc"),
        )
        return self.sections_to_text(sections)

    def _extract_tables(self, page: Any, page_num: int) -> list[ParsedSection]:
        """从 PDF 页面提取表格，转为 HTML <table> 标签。

        Args:
            page: pymupdf Page 对象。
            page_num: 页码。

        Returns:
            表格 ParsedSection 列表。find_tables 不可用或无表格时返回空列表。
        """
        try:
            table_finder = page.find_tables()
        except Exception as exc:
            log.debug("pdf.tables.find_failed", page=page_num, error=str(exc))
            return []

        if not table_finder or not table_finder.tables:
            return []

        sections: list[ParsedSection] = []
        for table in table_finder.tables:
            try:
                rows = table.extract()
            except Exception as exc:
                log.debug("pdf.tables.extract_failed", page=page_num, error=str(exc))
                continue

            html = self._rows_to_html(rows)
            if html:
                sections.append(
                    ParsedSection(kind="table", content=html, page=page_num)
                )

        return sections

    @staticmethod
    def _rows_to_html(rows: list[list[str | None]]) -> str:
        """将二维数据转为 HTML <table> 标签。

        第一行视为表头（<th>），其余为数据行（<td>）。
        None 值转为空字符串。
        """
        if not rows:
            return ""

        lines: list[str] = ["<table>"]

        for i, row in enumerate(rows):
            lines.append("<tr>")
            tag = "th" if i == 0 else "td"
            for cell in row:
                cell_text = (cell or "").strip()
                # 转义 HTML 特殊字符
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
        doc: Any,
        page: Any,
        page_num: int,
        remaining: int,
    ) -> tuple[list[ParsedSection], int]:
        """从 PDF 页面提取图片并调用 VLM 生成描述。

        Args:
            doc: pymupdf Document 对象。
            page: pymupdf Page 对象。
            page_num: 页码。
            remaining: 本文档剩余可提取图片数。

        Returns:
            (图片描述 ParsedSection 列表, 实际提取的图片数)
        """
        try:
            image_list = page.get_images(full=True)
        except Exception as exc:
            log.debug("pdf.images.get_failed", page=page_num, error=str(exc))
            return [], 0

        if not image_list:
            return [], 0

        # 限制数量
        image_list = image_list[:remaining]
        descriptions: list[ParsedSection] = []
        count = 0

        # 并发调用 VLM
        semaphore = asyncio.Semaphore(_VLM_SEMAPHORE_LIMIT)

        async def describe_image(xref: int) -> ParsedSection | None:
            try:
                img_info = doc.extract_image(xref)
            except Exception as exc:
                log.debug("pdf.images.extract_failed", xref=xref, error=str(exc))
                return None

            img_bytes = img_info.get("image")
            if not img_bytes:
                return None

            mime_type = img_info.get("ext", "png")
            # 标准化 mime type
            if mime_type == "jpg":
                mime_type = "jpeg"
            mime_type = f"image/{mime_type}"

            async with semaphore:
                desc = await self._vlm_describe(img_bytes, mime_type)

            if not desc or not desc.strip():
                return None

            return ParsedSection(
                kind="image_desc",
                content=f"[图片描述: {desc.strip()}]",
                page=page_num,
            )

        tasks = [describe_image(img[0]) for img in image_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, ParsedSection):
                descriptions.append(result)
                count += 1
            elif isinstance(result, Exception):
                log.warning("pdf.images.vlm_error", error=str(result))

        return descriptions, count

    async def _vlm_describe(self, image: bytes, mime_type: str) -> str:
        """调用 VLM 理解图片内容 — 延迟导入，VLM 不可用时返回空字符串。

        Args:
            image: 图片二进制数据。
            mime_type: 图片 MIME 类型。

        Returns:
            VLM 生成的描述文本。VLM 不可用时返回空字符串。
        """
        try:
            from app.vlm.provider import get_vision_provider

            vlm = get_vision_provider()
            return await vlm.understand(
                image=image,
                prompt=_IMAGE_PROMPT,
                mime_type=mime_type,
            )
        except ImportError:
            log.warning("pdf.vlm_not_available")
            return ""
        except Exception as exc:
            log.warning("pdf.vlm_error", error=str(exc))
            return ""
