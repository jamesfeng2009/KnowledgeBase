"""
PDF 文档解析器 — 单一职责：将 PDF 解析为增强文本（文本 + HTML 表格 + 图片描述/URL）。

四阶段能力：
    Phase 1: 表格提取 — pymupdf page.find_tables() → HTML <table> 标签；
    Phase 2: 图片提取 — 小图过滤 + 格式校验 + 上传 MinIO 保留 URL（对齐图片流程）；
    Phase 3: 图片描述 — VLM.understand() → [图片描述: ...]（可与 Phase 2 共存）；
    Phase 4: 扫描页 OCR — get_text() 返回空时，页面渲染为图片 → VLM OCR。

图片处理模式（由配置开关控制）：
    - PDF_IMAGE_UPLOAD_ENABLED=True: 上传 MinIO 保留 URL → kind="image_url"
      + VLM 描述（VLM 可用时同时生成描述，附在 URL 旁）
    - PDF_IMAGE_UPLOAD_ENABLED=False: 仅 VLM 文本描述 → kind="image_desc"（默认）
    - PDF_IMAGE_MIN_SIZE: 小图过滤阈值（宽或高 < 50px 跳过）

遵循优雅降级：
    - pymupdf 未安装 / find_tables 不可用 → 退化为 page.get_text() 纯文本；
    - VLM 不可用 → 跳过图片描述和扫描页 OCR，只保留文本和表格；
    - MinIO 不可用 → 跳过图片上传，降级为 VLM 文本描述；
    - 配置开关可单独关闭表格提取、图片提取或扫描页 OCR。
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
# 扫描页 OCR prompt — 提取页面所有文字，保持阅读顺序
_OCR_PROMPT: str = (
    "请提取这张扫描文档图片中的所有文字内容，"
    "保持原始的阅读顺序和段落结构，便于后续检索。"
    "如果有表格，请用文字描述表格内容。"
)


class PDFParser(DocumentParser):
    """PDF 解析器 — 文本 + 表格 + 图片 URL/VLM 描述 + 扫描页 OCR。

    使用 pymupdf (fitz) 完成所有提取，无额外依赖。
    """

    async def parse(self, file_path: str) -> str:
        """解析 PDF 文档，返回增强文本。

        Args:
            file_path: PDF 文件路径。

        Returns:
            增强文本（纯文本 + HTML 表格 + 图片描述/URL）。
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
        # 使用 _bool()/_int() 辅助函数处理 MagicMock（测试场景）和真实 Settings
        table_enabled = self._bool(getattr(settings, "PDF_TABLE_EXTRACTION_ENABLED", True), True)
        image_enabled = self._bool(getattr(settings, "PDF_IMAGE_EXTRACTION_ENABLED", True), True)
        max_images = self._int(getattr(settings, "PDF_IMAGE_MAX_PER_DOC", 50), 50)
        scan_ocr_enabled = self._bool(getattr(settings, "PDF_SCAN_OCR_ENABLED", True), True)
        scan_ocr_max_pages = self._int(getattr(settings, "PDF_SCAN_OCR_MAX_PAGES", 20), 20)
        image_upload_enabled = self._bool(getattr(settings, "PDF_IMAGE_UPLOAD_ENABLED", False), False)
        image_min_size = self._int(getattr(settings, "PDF_IMAGE_MIN_SIZE", 50), 50)

        sections: list[ParsedSection] = []
        image_count = 0
        scan_ocr_count = 0

        try:
            for page_num in range(len(doc)):
                page = doc[page_num]

                # 1. 提取文本
                text = page.get_text().strip()
                if text:
                    sections.append(
                        ParsedSection(kind="text", content=text, page=page_num)
                    )
                elif scan_ocr_enabled and scan_ocr_count < scan_ocr_max_pages:
                    # 扫描页 — get_text() 返回空，渲染页面为图片做 VLM OCR
                    ocr_text = await self._scan_page_ocr(doc, page, page_num)
                    if ocr_text:
                        sections.append(
                            ParsedSection(
                                kind="text",
                                content=f"[扫描页 OCR]\n{ocr_text}",
                                page=page_num,
                            )
                        )
                        scan_ocr_count += 1
                        log.info("pdf.scan_ocr_success", page=page_num)

                # 2. 提取表格（Phase 1）
                if table_enabled:
                    table_sections = self._extract_tables(page, page_num)
                    sections.extend(table_sections)

                # 3. 提取图片（Phase 2 + Phase 3）
                if image_enabled and image_count < max_images:
                    img_sections, img_count = await self._extract_images(
                        doc,
                        page,
                        page_num,
                        max_images - image_count,
                        image_upload_enabled=image_upload_enabled,
                        image_min_size=image_min_size,
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
            images=sum(1 for s in sections if s.kind in ("image_desc", "image_url")),
            scan_ocr=scan_ocr_count,
        )

        # 使用配置中的输出格式和分页分隔符
        output_format_raw = getattr(settings, "PARSER_OUTPUT_FORMAT", "html")
        output_format = output_format_raw if isinstance(output_format_raw, str) else "html"
        page_sep_raw = getattr(settings, "PAGE_SEPARATOR", "")
        page_separator = page_sep_raw if isinstance(page_sep_raw, str) else ""
        return self.sections_to_text(
            sections,
            output_format=output_format,
            page_separator=page_separator,
        )

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
        image_upload_enabled: bool = False,
        image_min_size: int = 50,
    ) -> tuple[list[ParsedSection], int]:
        """从 PDF 页面提取图片 — 支持上传 MinIO + VLM 描述 + 小图过滤。

        处理模式：
            - image_upload_enabled=True: 上传 MinIO → kind="image_url"
              + VLM 描述（VLM 可用时同时生成）
            - image_upload_enabled=False: 仅 VLM 描述 → kind="image_desc"
            - image_min_size: 宽或高小于此值的图片跳过

        Args:
            doc: pymupdf Document 对象。
            page: pymupdf Page 对象。
            page_num: 页码。
            remaining: 本文档剩余可提取图片数。
            image_upload_enabled: 是否上传图片到 MinIO。
            image_min_size: 最小尺寸阈值（宽或高小于此值跳过）。

        Returns:
            (图片 ParsedSection 列表, 实际提取的图片数)
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
        sections: list[ParsedSection] = []
        count = 0

        # VLM 并发控制
        semaphore = asyncio.Semaphore(_VLM_SEMAPHORE_LIMIT)

        async def process_image(idx: int, xref: int) -> ParsedSection | None:
            """处理单张图片 — 提取、过滤、上传/描述。"""
            try:
                img_info = doc.extract_image(xref)
            except Exception as exc:
                log.debug("pdf.images.extract_failed", xref=xref, error=str(exc))
                return None

            img_bytes = img_info.get("image")
            if not img_bytes:
                return None

            ext = img_info.get("ext", "png")
            img_width = img_info.get("width", 0) or 0
            img_height = img_info.get("height", 0) or 0

            # --- 小图过滤（仅在尺寸已知时过滤） ---
            if image_min_size > 0:
                from app.document.image_storage import get_image_dimensions

                if img_width == 0 or img_height == 0:
                    img_width, img_height = get_image_dimensions(img_bytes, ext)
                # 仅当尺寸确认大于 0 时才比较（未知尺寸的图片不过滤）
                if img_width > 0 and img_height > 0:
                    if img_width < image_min_size or img_height < image_min_size:
                        log.debug(
                            "pdf.images.filtered_small",
                            xref=xref,
                            width=img_width,
                            height=img_height,
                            min_size=image_min_size,
                        )
                        return None

            # 标准化扩展名
            std_ext = ext.lower().lstrip(".")
            if std_ext == "jpg":
                std_ext = "jpeg"
            mime_type = f"image/{std_ext}"

            # --- VLM 描述（两种模式都尝试） ---
            vlm_desc = ""
            async with semaphore:
                vlm_desc = await self._vlm_describe(img_bytes, mime_type)

            desc_text = ""
            if vlm_desc and vlm_desc.strip():
                desc_text = f"[图片描述: {vlm_desc.strip()}]"

            # --- 图片上传模式 ---
            if image_upload_enabled:
                from app.document.image_storage import upload_image

                url = await upload_image(
                    image_bytes=img_bytes,
                    ext=std_ext,
                    doc_id=getattr(page, "_doc_id", "unknown"),
                    page=page_num,
                    idx=idx,
                    min_size=0,  # 已过滤，不再重复
                    width=img_width,
                    height=img_height,
                )
                if url:
                    return ParsedSection(
                        kind="image_url",
                        content=desc_text,
                        page=page_num,
                        image_url=url,
                    )
                # 上传失败，降级为描述模式
                if desc_text:
                    return ParsedSection(
                        kind="image_desc",
                        content=desc_text,
                        page=page_num,
                    )
                return None

            # --- 仅描述模式（当前默认） ---
            if not desc_text:
                return None
            return ParsedSection(
                kind="image_desc",
                content=desc_text,
                page=page_num,
            )

        tasks = [process_image(idx, img[0]) for idx, img in enumerate(image_list)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, ParsedSection):
                sections.append(result)
                count += 1
            elif isinstance(result, Exception):
                log.warning("pdf.images.process_error", error=str(result))

        return sections, count

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

    async def _scan_page_ocr(
        self, doc: Any, page: Any, page_num: int
    ) -> str:
        """扫描页 OCR — 将页面渲染为图片，调用 VLM 提取文字。

        当 page.get_text() 返回空字符串时（扫描 PDF / 图片型 PDF），
        使用 pymupdf 的 page.get_pixmap() 将页面渲染为 PNG 图片，
        然后调用 VLM 进行 OCR 文字提取。

        Args:
            doc: pymupdf Document 对象。
            page: pymupdf Page 对象。
            page_num: 页码（用于日志）。

        Returns:
            OCR 提取的文本。VLM 不可用或渲染失败时返回空字符串。
        """
        # 1. 渲染页面为 PNG 图片
        try:
            import fitz

            # 2x 缩放提高 OCR 精度
            matrix = fitz.Matrix(2, 2)
            pixmap = page.get_pixmap(matrix=matrix)
            img_bytes: bytes = pixmap.tobytes("png")
        except Exception as exc:
            log.debug("pdf.scan_ocr_render_failed", page=page_num, error=str(exc))
            return ""

        if not img_bytes:
            return ""

        # 2. 调用 VLM OCR
        try:
            from app.vlm.provider import get_vision_provider

            vlm = get_vision_provider()
            ocr_text = await vlm.understand(
                image=img_bytes,
                prompt=_OCR_PROMPT,
                mime_type="image/png",
            )
            return ocr_text or ""
        except ImportError:
            log.warning("pdf.scan_ocr_vlm_not_available")
            return ""
        except Exception as exc:
            log.warning("pdf.scan_ocr_error", page=page_num, error=str(exc))
            return ""
