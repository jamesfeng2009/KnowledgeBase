"""PDF 文档解析器 — 单一职责：将 PDF 解析为增强文本（文本 + HTML 表格 + 图片描述/URL）。

四阶段能力：
    Phase 1: 表格提取 — pymupdf page.find_tables() → HTML <table> 标签；
    Phase 2: 图片提取 — 小图过滤 + 格式校验 + 上传 MinIO 保留 URL（对齐图片流程）；
    Phase 3: 图片描述 — VLM.understand() → [图片描述: ...]（可与 Phase 2 共存）；
    Phase 4: 扫描页 OCR — get_text() 返回空时，页面渲染为图片 → VLM OCR。

版式分层（Phase 0，借鉴 pdfminer 二次开发思路）：
    文本提取前，先用 get_text("dict") 拿 block 坐标，
    经 layout_analyzer 重建阅读顺序（栏检测 + 通栏处理），
    解决复杂多栏版式的乱序问题。性能不变（C 层提取 + Python 排序）。
    开关 PDF_LAYOUT_ANALYSIS_ENABLED，异常时自动降级为 get_text()。

图片处理模式（由配置开关控制）：
    - PDF_IMAGE_UPLOAD_ENABLED=True（D6 默认）: 上传 MinIO 保留 URL → kind="image_url"
      + VLM 描述（VLM 可用时同时生成描述，附在 URL 旁）
    - PDF_IMAGE_UPLOAD_ENABLED=False: 仅 VLM 文本描述 → kind="image_desc"
    - PDF_IMAGE_MIN_SIZE: 小图过滤阈值（宽或高 < 50px 跳过）

遵循优雅降级：
    - pymupdf 未安装 / find_tables 不可用 → 退化为 page.get_text() 纯文本；
    - 版式分析异常 → 退化为 page.get_text()（保内容不保顺序）；
    - VLM 不可用 → 跳过图片描述和扫描页 OCR，只保留文本和表格；
    - MinIO 不可用 → 跳过图片上传，降级为 VLM 文本描述；
    - 配置开关可单独关闭表格提取、图片提取、扫描页 OCR 或版式分析。
"""

from __future__ import annotations

import asyncio
from html import escape
from typing import Any

from app.config import get_settings
from app.core.parse_config import get_parse_settings
from app.document.base import DocumentParser, ParsedSection
from app.utils.logger import get_logger

log = get_logger(__name__)


class EncryptedPDFError(ValueError):
    """PDF 已加密且无法用空密码解密 — 必须显式失败，不得静默降级为空文本。"""

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

    def __init__(self) -> None:
        self.last_parse_warnings: list[str] = []
        self.last_scan_ocr_count: int = 0

    async def parse(self, file_path: str, doc_id: str | None = None) -> str:
        """解析 PDF 文档，返回增强文本。

        Args:
            file_path: PDF 文件路径。
            doc_id: 文档 ID — 图片上传的对象命名空间，防止跨文档覆盖。

        Returns:
            增强文本（纯文本 + HTML 表格 + 图片描述/URL）。
            pymupdf 未安装或解析失败时返回空字符串。

        Raises:
            EncryptedPDFError: PDF 已加密且无法解密。
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

        if doc.needs_pass and not doc.authenticate(""):
            doc.close()
            raise EncryptedPDFError("PDF 已加密，无法解析，请解密后重新上传")

        settings = get_parse_settings()
        # 使用 _bool()/_int() 辅助函数处理 MagicMock（测试场景）和真实 Settings
        table_enabled = self._bool(getattr(settings, "PDF_TABLE_EXTRACTION_ENABLED", True), True)
        image_enabled = self._bool(getattr(settings, "PDF_IMAGE_EXTRACTION_ENABLED", True), True)
        max_images = self._int(getattr(settings, "PDF_IMAGE_MAX_PER_DOC", 50), 50)
        scan_ocr_enabled = self._bool(getattr(settings, "PDF_SCAN_OCR_ENABLED", True), True)
        scan_ocr_max_pages = self._int(getattr(settings, "PDF_SCAN_OCR_MAX_PAGES", 20), 20)
        image_upload_enabled = self._bool(getattr(settings, "PDF_IMAGE_UPLOAD_ENABLED", False), False)
        image_min_size = self._int(getattr(settings, "PDF_IMAGE_MIN_SIZE", 50), 50)
        layout_analysis_enabled = self._bool(
            getattr(settings, "PDF_LAYOUT_ANALYSIS_ENABLED", True), True
        )
        hf_enabled = self._bool(
            getattr(settings, "PDF_HEADER_FOOTER_REMOVAL_ENABLED", True), True
        )
        hf_min_pages = self._int(
            getattr(settings, "PDF_HEADER_FOOTER_MIN_PAGES", 3), 3
        )
        toc_enabled = self._bool(
            getattr(settings, "PDF_TOC_HEADINGS_ENABLED", True), True
        )

        sections: list[ParsedSection] = []
        image_count = 0
        scan_ocr_count = 0
        self.last_parse_warnings: list[str] = []
        scan_min_chars = self._int(
            getattr(settings, "PDF_SCAN_OCR_MIN_TEXT_CHARS", 32), 32
        )
        # close 之后 pymupdf Document 不可再访问（len/bool 均抛 ValueError），
        # 页数必须在关闭前缓存。
        page_count = len(doc)
        # A12: 真实页数元数据 — 供调用方落库 page_count，不再按 <h2> 数推断
        self.last_page_count = page_count

        # D2: 书签真值 — 页码 → [(层级, 标题)]，按页注入 <h> 标签
        toc_headings: dict[int, list[tuple[int, str]]] = {}
        if toc_enabled:
            try:
                for level, title, page_no in doc.get_toc(simple=True):
                    title = (title or "").strip()
                    if not title or page_no < 1:
                        continue
                    lvl = min(max(int(level), 1), 6)
                    idx = min(page_no - 1, page_count - 1)
                    toc_headings.setdefault(idx, []).append((lvl, title))
            except Exception as exc:
                log.debug("pdf.toc_read_failed", error=str(exc))

        # A10: 跨页页眉页脚预检测（同文本同位置 >= N 页的顶/底块）
        page_dicts: list[dict[str, Any]] = []
        hf_keys: set[tuple] = set()
        if layout_analysis_enabled and hf_enabled and page_count >= hf_min_pages:
            try:
                from app.document.layout_analyzer import detect_header_footer_keys

                page_dicts = [doc[i].get_text("dict") for i in range(page_count)]
                hf_keys = detect_header_footer_keys(page_dicts, hf_min_pages)
            except Exception as exc:
                log.debug("pdf.hf_detect_failed", error=str(exc))
                page_dicts = []

        try:
            for page_num in range(page_count):
                page = doc[page_num]

                # 1. 先提取表格（Phase 1）— A13: 正文需按表格 bbox 剔除重复文字
                table_sections: list[ParsedSection] = []
                table_bboxes: list[tuple[float, float, float, float]] = []
                if table_enabled:
                    table_sections, table_bboxes = self._extract_tables(page, page_num)

                # 2. 提取文本（Phase 0: 版式分层 + 页眉页脚剔除 + 表格区域去重）
                text = self._extract_text_ordered(
                    page,
                    layout_analysis_enabled,
                    page_dict=page_dicts[page_num] if page_dicts else None,
                    exclude_bboxes=table_bboxes,
                    hf_keys=hf_keys,
                )
                # 表格文字计入扫描判定，避免纯表格页被误判为扫描页触发 OCR
                table_chars = sum(len(s.content) for s in table_sections)
                # D2: 书签标题注入 — 正文重复标题行去重，标题字符计入扫描判定
                page_toc = toc_headings.get(page_num, [])
                toc_chars = 0
                if page_toc:
                    titles = [t for _, t in page_toc]
                    text = self._dedup_toc_lines(text, titles)
                    toc_chars = sum(len(t) for t in titles)
                    for lvl, title in page_toc:
                        sections.append(
                            ParsedSection(
                                kind="text",
                                content=f"<h{lvl}>{escape(title)}</h{lvl}>",
                                page=page_num,
                            )
                        )
                # 文本覆盖率低于阈值 → 扫描主导页（含带少量文本层的混合扫描页）
                if scan_ocr_enabled and len(text.strip()) + table_chars + toc_chars < scan_min_chars:
                    if scan_ocr_count < scan_ocr_max_pages:
                        scan_ocr_count += 1
                        ocr_text = await self._scan_page_ocr(doc, page, page_num)
                        if ocr_text:
                            sections.append(
                                ParsedSection(
                                    kind="text",
                                    content=f"[扫描页 OCR]\n{ocr_text}",
                                    page=page_num,
                                )
                            )
                            log.info("pdf.scan_ocr_success", page=page_num)
                        elif text.strip():
                            sections.append(
                                ParsedSection(kind="text", content=text, page=page_num)
                            )
                    else:
                        self.last_parse_warnings.append(
                            f"第{page_num + 1}页疑似扫描页，超出扫描页 OCR 上限"
                            f"（{scan_ocr_max_pages}页），未提取该页文字"
                        )
                        if text.strip():
                            sections.append(
                                ParsedSection(kind="text", content=text, page=page_num)
                            )
                elif text:
                    sections.append(
                        ParsedSection(kind="text", content=text, page=page_num)
                    )

                # 表格片段排在同页文本之后（保持原有输出顺序）
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
                        doc_id=doc_id,
                    )
                    sections.extend(img_sections)
                    image_count += img_count

        finally:
            doc.close()

        # D8: 扫描 OCR 页数元数据 — 供调用方落 parse_meta.ocr_pages
        self.last_scan_ocr_count = scan_ocr_count

        log.info(
            "pdf.parsed",
            file_path=file_path,
            pages=page_count,
            sections=len(sections),
            tables=sum(1 for s in sections if s.kind == "table"),
            images=sum(1 for s in sections if s.kind in ("image_desc", "image_url")),
            scan_ocr=scan_ocr_count,
        )

        # 使用配置中的分页分隔符
        page_sep_raw = getattr(settings, "PAGE_SEPARATOR", "")
        page_separator = page_sep_raw if isinstance(page_sep_raw, str) else ""
        return self.sections_to_text(
            sections,
            page_separator=page_separator,
        )

    @staticmethod
    def _dedup_toc_lines(text: str, titles: list[str]) -> str:
        """剔除正文中与书签标题重复的行（每个标题至多剔除一次）。"""
        if not text:
            return text
        remaining = list(titles)
        out: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped in remaining:
                remaining.remove(stripped)
                continue
            out.append(line)
        return "\n".join(out)

    def _extract_text_ordered(
        self,
        page: Any,
        layout_analysis_enabled: bool,
        page_dict: dict[str, Any] | None = None,
        exclude_bboxes: list[tuple[float, float, float, float]] | None = None,
        hf_keys: set[tuple] | None = None,
    ) -> str:
        """提取页面文本，优先经版式分析层重建阅读顺序。

        版式分析（layout_analyzer）基于 get_text("dict") 的 block 坐标做
        栏检测与阅读顺序排序，解决多栏版式乱序；任何异常降级为
        page.get_text()，保证内容不丢（仅可能不保顺序）。

        Args:
            page: pymupdf Page 对象。
            layout_analysis_enabled: 是否启用版式分析（配置开关）。
            page_dict: 预取的 get_text("dict") 结果（页眉页脚预检测复用）。
            exclude_bboxes: 表格区域 — 中心落入的 block 剔除（A13 去重）。
            hf_keys: 页眉页脚 key 集合 — 命中的 block 剔除（A10）。

        Returns:
            页面纯文本（已 strip）。
        """
        if layout_analysis_enabled:
            try:
                from app.document.layout_analyzer import extract_page_text_ordered

                pd = page_dict if page_dict is not None else page.get_text("dict")
                ordered = extract_page_text_ordered(
                    pd,
                    float(page.rect.width),
                    page_height=float(page.rect.height),
                    exclude_bboxes=exclude_bboxes or None,
                    hf_keys=hf_keys or None,
                )
                if ordered.strip():
                    return ordered.strip()
                if exclude_bboxes or hf_keys:
                    # 内容被表格去重/页眉页脚规则全部剔除 — 不得回退
                    # get_text()，否则被剔除的文字会重新混入正文
                    return ""
            except Exception as exc:
                # 版式分析失败不致命 —— 降级为纯文本提取
                log.debug("pdf.layout_fallback", error=str(exc))
        return page.get_text().strip()

    def _extract_tables(
        self, page: Any, page_num: int
    ) -> tuple[list[ParsedSection], list[tuple[float, float, float, float]]]:
        """从 PDF 页面提取表格，转为 HTML <table> 标签。

        Args:
            page: pymupdf Page 对象。
            page_num: 页码。

        Returns:
            (表格 ParsedSection 列表, 表格 bbox 列表)。
            bbox 供正文按区域去重（A13）。find_tables 不可用或无表格时返回 ([], [])。
        """
        try:
            table_finder = page.find_tables()
        except Exception as exc:
            log.debug("pdf.tables.find_failed", page=page_num, error=str(exc))
            return [], []

        if not table_finder or not table_finder.tables:
            return [], []

        sections: list[ParsedSection] = []
        bboxes: list[tuple[float, float, float, float]] = []
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
                try:
                    bbox = tuple(float(v) for v in table.bbox)
                    if len(bbox) == 4:
                        bboxes.append(bbox)
                except Exception:
                    pass

        return sections, bboxes

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
        doc_id: str | None = None,
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
                    doc_id=doc_id or "unknown",
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
        """调用 VLM 理解图片内容 — 延迟导入，VLM 不可用/失败时返回空字符串。

        Args:
            image: 图片二进制数据。
            mime_type: 图片 MIME 类型。

        Returns:
            VLM 生成的描述文本。VLM 不可用或失败时返回空字符串并记 warning。
        """
        try:
            from app.vlm.provider import get_vision_provider

            vlm = get_vision_provider()
            result = await vlm.understand(
                image=image,
                prompt=_IMAGE_PROMPT,
                mime_type=mime_type,
            )
            if result is None:
                log.warning("pdf.vlm_failed")
                self.last_parse_warnings.append("图片 VLM 描述失败，已跳过该图片描述")
                return ""
            return result
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
            if ocr_text is None:
                log.warning("pdf.scan_ocr_failed", page=page_num)
                self.last_parse_warnings.append(
                    f"扫描页 OCR 失败（第 {page_num + 1} 页），该页文字未提取"
                )
                return ""
            return ocr_text
        except ImportError:
            log.warning("pdf.scan_ocr_vlm_not_available")
            return ""
        except Exception as exc:
            log.warning("pdf.scan_ocr_error", page=page_num, error=str(exc))
            return ""
