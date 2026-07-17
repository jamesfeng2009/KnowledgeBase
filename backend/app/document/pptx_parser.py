"""
PPTX 文档解析器 — 单一职责：将 PPT 解析为增强文本（文本 + HTML 表格 + 图片描述）。

使用 python-pptx 提取幻灯片文本、表格和内嵌图片：
    - 文本：shape.text（文本框、占位符）
    - 表格：shape.has_table → HTML <table>
    - 图片：shape.shape_type == PICTURE → VLM 描述

每个 slide 输出为 <h2>幻灯片 N: 标题</h2> + 内容，
chunker 的 _split_html 天然按 slide 分块。

不引入 LibreOffice headless（重型依赖），不做整页截图渲染。
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


class PPTXParser(DocumentParser):
    """PPTX 解析器 — python-pptx 文本 + 表格 + 内嵌图片 VLM。"""

    async def parse(self, file_path: str) -> str:
        """解析 PPTX 文档，返回增强文本。

        Args:
            file_path: PPTX 文件路径。

        Returns:
            增强文本。python-pptx 未安装或解析失败时返回空字符串。
        """
        try:
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE
        except ImportError:
            log.warning("pptx.parser_skipped", reason="python-pptx not installed")
            return ""

        try:
            prs = Presentation(file_path)
        except Exception as exc:
            log.warning("pptx.open_failed", file_path=file_path, error=str(exc))
            return ""

        settings = get_settings()
        image_enabled = getattr(settings, "PPTX_IMAGE_EXTRACTION_ENABLED", True)
        max_images = getattr(settings, "PPTX_IMAGE_MAX_PER_DOC", 50)

        sections: list[ParsedSection] = []
        image_count = 0
        semaphore = asyncio.Semaphore(_VLM_SEMAPHORE_LIMIT)

        for slide_num, slide in enumerate(prs.slides):
            slide_sections = self._extract_slide_text(slide, slide_num)
            sections.extend(slide_sections)

            # 提取表格
            table_sections = self._extract_tables(slide, slide_num)
            sections.extend(table_sections)

            # 提取图片
            if image_enabled and image_count < max_images:
                img_sections, img_count = await self._extract_images(
                    slide, slide_num, MSO_SHAPE_TYPE, semaphore,
                    max_images - image_count,
                )
                sections.extend(img_sections)
                image_count += img_count

            # 提取演讲者备注
            note_section = self._extract_notes(slide, slide_num)
            if note_section:
                sections.append(note_section)

        log.info(
            "pptx.parsed",
            file_path=file_path,
            slides=len(prs.slides),
            sections=len(sections),
            tables=sum(1 for s in sections if s.kind == "table"),
            images=sum(1 for s in sections if s.kind == "image_desc"),
        )
        return self.sections_to_text(sections)

    def _extract_slide_text(self, slide: Any, slide_num: int) -> list[ParsedSection]:
        """提取幻灯片文本，包装为 <h2> 标题块。

        尝试从 slide 的第一个标题占位符提取标题，找不到则用"幻灯片 N"。
        递归遍历 GROUP 组合形状的子形状，提取组合内的文本框。
        """
        title = self._get_slide_title(slide) or f"幻灯片 {slide_num + 1}"
        text_parts: list[str] = [f"<h2>{title}</h2>"]

        # 递归遍历所有形状（包括 GROUP 组合形状的子形状）
        for shape in slide.shapes:
            self._collect_shape_text(shape, text_parts)

        content = "\n".join(text_parts).strip()
        if not content:
            return []

        return [ParsedSection(kind="text", content=content, page=slide_num)]

    def _collect_shape_text(
        self, shape: Any, text_parts: list[str], depth: int = 0
    ) -> None:
        """递归收集形状文本 — 处理 GROUP 组合形状。

        GROUP 形状本身没有 text_frame，但其子形状可能有。
        递归遍历子形状提取文本，同时跳过表格和图片（由专用方法处理）。

        Args:
            shape: pptx Shape 对象。
            text_parts: 文本收集列表（可变引用）。
            depth: 递归深度（防止无限循环，上限 5 层）。
        """
        if depth > 5:
            return

        # 跳过表格（由 _extract_tables 处理）
        if shape.has_table:
            return

        # 检查是否为图片（PICTURE = 13）
        try:
            shape_type = shape.shape_type
        except Exception:
            shape_type = None
        if shape_type is not None and hasattr(shape_type, "value") and shape_type.value == 13:
            return

        # 检查是否为 GROUP 形状（GROUP = 6）
        if shape_type is not None and hasattr(shape_type, "value") and shape_type.value == 6:
            try:
                for child_shape in shape.shapes:
                    self._collect_shape_text(child_shape, text_parts, depth + 1)
            except Exception as exc:
                log.debug("pptx.group_traverse_failed", slide=depth, error=str(exc))
            return

        # 普通形状 — 提取文本
        if shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if text:
                text_parts.append(text)

    @staticmethod
    def _get_slide_title(slide: Any) -> str:
        """尝试从幻灯片提取标题文本。"""
        try:
            if slide.shapes.title:
                return slide.shapes.title.text.strip()
        except Exception:
            pass

        # 尝试从 placeholders 提取
        try:
            for placeholder in slide.placeholders:
                if placeholder.placeholder_format.idx == 0:  # 标题占位符
                    return placeholder.text.strip()
        except Exception:
            pass

        return ""

    @staticmethod
    def _extract_notes(slide: Any, slide_num: int) -> ParsedSection | None:
        """提取幻灯片的演讲者备注。

        备注存储在 slide.notes_slide.notes_text_frame 中，
        包含演讲者补充说明，对 RAG 检索有较高价值。

        Args:
            slide: pptx Slide 对象。
            slide_num: 幻灯片编号。

        Returns:
            备注的 ParsedSection，无备注时返回 None。
        """
        try:
            # has_notes_slide 在某些版本中不可用，用 try/except 保护
            if not slide.has_notes_slide:
                return None
        except Exception:
            pass

        try:
            notes_slide = slide.notes_slide
            notes_text = notes_slide.notes_text_frame.text.strip()
            if not notes_text:
                return None

            return ParsedSection(
                kind="text",
                content=f"[演讲者备注]\n{notes_text}",
                page=slide_num,
            )
        except Exception as exc:
            log.debug("pptx.notes_extract_failed", slide=slide_num, error=str(exc))
            return None

    def _extract_tables(self, slide: Any, slide_num: int) -> list[ParsedSection]:
        """提取幻灯片中的表格，转为 HTML <table>。"""
        sections: list[ParsedSection] = []

        for shape in slide.shapes:
            if not shape.has_table:
                continue

            try:
                table = shape.table
                rows: list[list[str | None]] = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows.append(cells)

                html = self._rows_to_html(rows)
                if html:
                    sections.append(
                        ParsedSection(kind="table", content=html, page=slide_num)
                    )
            except Exception as exc:
                log.debug("pptx.table_extract_failed", slide=slide_num, error=str(exc))

        return sections

    @staticmethod
    def _rows_to_html(rows: list[list[str | None]]) -> str:
        """将二维数据转为 HTML <table> 标签 — 与 PDFParser 逻辑一致。

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
        slide: Any,
        slide_num: int,
        mso_shape_type: Any,
        semaphore: asyncio.Semaphore,
        remaining: int,
    ) -> tuple[list[ParsedSection], int]:
        """提取幻灯片内嵌图片，调用 VLM 生成描述。"""
        sections: list[ParsedSection] = []
        count = 0

        # PICTURE = 13
        picture_type = 13
        try:
            # MSO_SHAPE_TYPE.PICTURE 的 value 是 13
            picture_type = mso_shape_type.PICTURE
            picture_val = getattr(picture_type, "value", 13)
        except Exception:
            picture_val = 13

        image_shapes = []
        for shape in slide.shapes:
            if count >= remaining:
                break
            try:
                shape_type = shape.shape_type
            except Exception:
                continue
            if shape_type is not None and hasattr(shape_type, "value") and shape_type.value == picture_val:
                image_shapes.append(shape)

        async def describe_shape(shape: Any) -> ParsedSection | None:
            try:
                img_bytes = shape.image.blob
                mime_type = "image/png"
                try:
                    ext = shape.image.ext
                    if ext == "jpg":
                        ext = "jpeg"
                    mime_type = f"image/{ext}"
                except Exception:
                    pass
            except Exception as exc:
                log.debug("pptx.image_extract_failed", slide=slide_num, error=str(exc))
                return None

            async with semaphore:
                desc = await self._vlm_describe(img_bytes, mime_type)

            if not desc or not desc.strip():
                return None

            return ParsedSection(
                kind="image_desc",
                content=f"[图片描述: {desc.strip()}]",
                page=slide_num,
            )

        tasks = [describe_shape(shape) for shape in image_shapes[:remaining]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, ParsedSection):
                sections.append(result)
                count += 1
            elif isinstance(result, Exception):
                log.warning("pptx.image_vlm_error", error=str(result))

        return sections, count

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
            log.warning("pptx.vlm_not_available")
            return ""
        except Exception as exc:
            log.warning("pptx.vlm_error", error=str(exc))
            return ""

