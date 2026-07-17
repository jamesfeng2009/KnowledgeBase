"""
文档解析器测试 — app/document/ 模块。

覆盖范围：
    - ParsedSection / DocumentParser 基类
    - PDFParser：表格提取、图片 VLM、降级、配置开关
    - PPTXParser：文本提取、表格、图片 VLM、降级
    - factory：get_parser 路由
    - document_tasks 集成：_parse_pdf / _parse_pptx 路由
    - config 配置项
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock celery 模块（测试环境未安装 celery）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery

# Mock celery_app 模块（避免实际 Celery 实例化）
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# 基类测试
# ======================================================================


class TestParsedSection:
    """ParsedSection 数据类测试。"""

    def test_creation(self) -> None:
        from app.document.base import ParsedSection

        sec = ParsedSection(kind="text", content="hello", page=1)
        assert sec.kind == "text"
        assert sec.content == "hello"
        assert sec.page == 1

    def test_default_page(self) -> None:
        from app.document.base import ParsedSection

        sec = ParsedSection(kind="table", content="<table></table>")
        assert sec.page == 0


class TestSectionsToText:
    """sections_to_text 合并测试。"""

    def test_empty(self) -> None:
        from app.document.base import DocumentParser

        assert DocumentParser.sections_to_text([]) == ""

    def test_single_section(self) -> None:
        from app.document.base import DocumentParser, ParsedSection

        sections = [ParsedSection(kind="text", content="hello world", page=0)]
        assert DocumentParser.sections_to_text(sections) == "hello world"

    def test_multiple_sections_sorted_by_page(self) -> None:
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(kind="text", content="page2", page=2),
            ParsedSection(kind="text", content="page0", page=0),
            ParsedSection(kind="table", content="<table></table>", page=1),
        ]
        result = DocumentParser.sections_to_text(sections)
        # 按页码排序
        assert result.index("page0") < result.index("<table>") < result.index("page2")

    def test_empty_content_skipped(self) -> None:
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(kind="text", content="keep", page=0),
            ParsedSection(kind="text", content="  ", page=1),
            ParsedSection(kind="text", content="", page=2),
        ]
        assert DocumentParser.sections_to_text(sections) == "keep"


# ======================================================================
# PDFParser 测试
# ======================================================================


class TestPDFParserRowsToHtml:
    """PDF _rows_to_html 方法测试。"""

    def test_basic_table(self) -> None:
        from app.document.pdf_parser import PDFParser

        rows = [
            ["模块", "技术"],
            ["数据层", "PostgreSQL"],
        ]
        html = PDFParser._rows_to_html(rows)
        assert "<table>" in html
        assert "</table>" in html
        assert "<th>模块</th>" in html
        assert "<td>PostgreSQL</td>" in html

    def test_empty_rows(self) -> None:
        from app.document.pdf_parser import PDFParser

        assert PDFParser._rows_to_html([]) == ""

    def test_none_cells(self) -> None:
        from app.document.pdf_parser import PDFParser

        rows = [["a", None, "c"]]
        html = PDFParser._rows_to_html(rows)
        assert "<th>a</th>" in html
        assert "<th></th>" in html  # None → 空字符串
        assert "<th>c</th>" in html

    def test_html_escaping(self) -> None:
        from app.document.pdf_parser import PDFParser

        rows = [["<script>alert(1)</script>"]]
        html = PDFParser._rows_to_html(rows)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_ampersand_escaping(self) -> None:
        from app.document.pdf_parser import PDFParser

        rows = [["A & B"]]
        html = PDFParser._rows_to_html(rows)
        assert "A &amp; B" in html


class TestPDFParserExtractTables:
    """PDF 表格提取测试。"""

    def test_no_tables(self) -> None:
        """无表格时返回空列表。"""
        from app.document.pdf_parser import PDFParser

        parser = PDFParser()
        mock_page = MagicMock()
        mock_table_finder = MagicMock()
        mock_table_finder.tables = []
        mock_page.find_tables.return_value = mock_table_finder

        result = parser._extract_tables(mock_page, 0)
        assert result == []

    def test_find_tables_exception(self) -> None:
        """find_tables 异常时返回空列表。"""
        from app.document.pdf_parser import PDFParser

        parser = PDFParser()
        mock_page = MagicMock()
        mock_page.find_tables.side_effect = Exception("not supported")

        result = parser._extract_tables(mock_page, 0)
        assert result == []

    def test_table_extraction(self) -> None:
        """成功提取表格转为 HTML。"""
        from app.document.pdf_parser import PDFParser

        parser = PDFParser()
        mock_table = MagicMock()
        mock_table.extract.return_value = [
            ["姓名", "年龄"],
            ["张三", "25"],
        ]
        mock_table_finder = MagicMock()
        mock_table_finder.tables = [mock_table]
        mock_page = MagicMock()
        mock_page.find_tables.return_value = mock_table_finder

        result = parser._extract_tables(mock_page, 0)
        assert len(result) == 1
        assert result[0].kind == "table"
        assert "<th>姓名</th>" in result[0].content
        assert "<td>张三</td>" in result[0].content


class TestPDFParserParse:
    """PDF parse 完整流程测试。"""

    @pytest.mark.asyncio
    async def test_pymupdf_not_installed(self) -> None:
        """pymupdf 未安装时返回空字符串。"""
        from app.document.pdf_parser import PDFParser

        with patch.dict("sys.modules", {"fitz": None}):
            parser = PDFParser()
            result = await parser.parse("/fake/path.pdf")
            assert result == ""

    @pytest.mark.asyncio
    async def test_open_failed(self) -> None:
        """文件打开失败时返回空字符串。"""
        from app.document.pdf_parser import PDFParser

        mock_fitz = MagicMock()
        mock_fitz.open.side_effect = Exception("file not found")

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            parser = PDFParser()
            result = await parser.parse("/fake/path.pdf")
            assert result == ""

    @pytest.mark.asyncio
    async def test_parse_with_text_and_tables(self) -> None:
        """解析包含文本和表格的 PDF。"""
        from app.document.pdf_parser import PDFParser

        # 构建 mock pymupdf
        mock_page = MagicMock()
        mock_page.get_text.return_value = "这是第一页的文本内容"
        mock_table = MagicMock()
        mock_table.extract.return_value = [
            ["列A", "列B"],
            ["值1", "值2"],
        ]
        mock_table_finder = MagicMock()
        mock_table_finder.tables = [mock_table]
        mock_page.find_tables.return_value = mock_table_finder
        mock_page.get_images.return_value = []  # 无图片

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            parser = PDFParser()
            result = await parser.parse("/fake/doc.pdf")

        assert "这是第一页的文本内容" in result
        assert "<table>" in result
        assert "<th>列A</th>" in result
        assert "<td>值1</td>" in result

    @pytest.mark.asyncio
    async def test_parse_table_disabled(self) -> None:
        """表格提取关闭时只有文本。"""
        from app.document.pdf_parser import PDFParser

        mock_page = MagicMock()
        mock_page.get_text.return_value = "纯文本内容"
        mock_page.get_images.return_value = []

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fitz}), \
             patch("app.document.pdf_parser.get_settings") as mock_settings:
            mock_settings.return_value.PDF_TABLE_EXTRACTION_ENABLED = False
            mock_settings.return_value.PDF_IMAGE_EXTRACTION_ENABLED = False
            mock_settings.return_value.PDF_IMAGE_MAX_PER_DOC = 50

            parser = PDFParser()
            result = await parser.parse("/fake/doc.pdf")

        assert "纯文本内容" in result
        assert "<table>" not in result
        mock_page.find_tables.assert_not_called()

    @pytest.mark.asyncio
    async def test_parse_with_image_vlm(self) -> None:
        """解析包含图片的 PDF，VLM 生成描述。"""
        from app.document.pdf_parser import PDFParser

        mock_page = MagicMock()
        mock_page.get_text.return_value = "页面文本"
        mock_page.find_tables.return_value = MagicMock(tables=[])

        # 模拟图片
        mock_page.get_images.return_value = [(1, 0, 0, 0, 0, 0, 0)]

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()
        mock_doc.extract_image.return_value = {
            "image": b"\x89PNG fake bytes",
            "ext": "png",
        }

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        mock_vlm = MagicMock()
        mock_vlm.understand = AsyncMock(return_value="架构图显示三层结构")

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fitz}), \
             patch("app.document.pdf_parser.get_settings") as mock_settings, \
             patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm):
            mock_settings.return_value.PDF_TABLE_EXTRACTION_ENABLED = True
            mock_settings.return_value.PDF_IMAGE_EXTRACTION_ENABLED = True
            mock_settings.return_value.PDF_IMAGE_MAX_PER_DOC = 50

            parser = PDFParser()
            result = await parser.parse("/fake/doc.pdf")

        assert "页面文本" in result
        assert "[图片描述: 架构图显示三层结构]" in result
        # 验证 VLM 接收到 bytes
        mock_vlm.understand.assert_called_once()
        call_kwargs = mock_vlm.understand.call_args.kwargs
        assert isinstance(call_kwargs["image"], bytes)

    @pytest.mark.asyncio
    async def test_parse_image_max_limit(self) -> None:
        """图片超过上限时截断。"""
        from app.document.pdf_parser import PDFParser

        mock_page = MagicMock()
        mock_page.get_text.return_value = "文本"
        mock_page.find_tables.return_value = MagicMock(tables=[])
        # 10 张图片，上限 3
        mock_page.get_images.return_value = [(i, 0, 0, 0, 0, 0, 0) for i in range(10)]

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()
        mock_doc.extract_image.return_value = {
            "image": b"fake",
            "ext": "png",
        }

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        mock_vlm = MagicMock()
        mock_vlm.understand = AsyncMock(return_value="描述")

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fitz}), \
             patch("app.document.pdf_parser.get_settings") as mock_settings, \
             patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm):
            mock_settings.return_value.PDF_TABLE_EXTRACTION_ENABLED = False
            mock_settings.return_value.PDF_IMAGE_EXTRACTION_ENABLED = True
            mock_settings.return_value.PDF_IMAGE_MAX_PER_DOC = 3

            parser = PDFParser()
            result = await parser.parse("/fake/doc.pdf")

        # VLM 最多调用 3 次
        assert mock_vlm.understand.call_count <= 3

    @pytest.mark.asyncio
    async def test_parse_vlm_not_available(self) -> None:
        """VLM 不可用时跳过图片描述。"""
        from app.document.pdf_parser import PDFParser

        mock_page = MagicMock()
        mock_page.get_text.return_value = "文本"
        mock_page.find_tables.return_value = MagicMock(tables=[])
        mock_page.get_images.return_value = [(1, 0, 0, 0, 0, 0, 0)]

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()
        mock_doc.extract_image.return_value = {"image": b"fake", "ext": "png"}

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fitz}), \
             patch("app.document.pdf_parser.get_settings") as mock_settings, \
             patch.object(PDFParser, "_vlm_describe", AsyncMock(return_value="")):
            mock_settings.return_value.PDF_TABLE_EXTRACTION_ENABLED = False
            mock_settings.return_value.PDF_IMAGE_EXTRACTION_ENABLED = True
            mock_settings.return_value.PDF_IMAGE_MAX_PER_DOC = 50

            parser = PDFParser()
            result = await parser.parse("/fake/doc.pdf")

        # 文本保留，图片描述被跳过（_vlm_describe 返回空字符串）
        assert "文本" in result
        assert "[图片描述:" not in result


# ======================================================================
# PPTXParser 测试
# ======================================================================


class TestPPTXParserRowsToHtml:
    """PPTX _rows_to_html 方法测试。"""

    def test_basic_table(self) -> None:
        from app.document.pptx_parser import PPTXParser

        rows = [
            ["模块", "技术"],
            ["前端", "Astro"],
        ]
        html = PPTXParser._rows_to_html(rows)
        assert "<table>" in html
        assert "<th>模块</th>" in html
        assert "<td>Astro</td>" in html

    def test_empty_rows(self) -> None:
        from app.document.pptx_parser import PPTXParser

        assert PPTXParser._rows_to_html([]) == ""


class TestPPTXParserParse:
    """PPTX parse 完整流程测试。"""

    @pytest.mark.asyncio
    async def test_pptx_not_installed(self) -> None:
        """python-pptx 未安装时返回空字符串。"""
        from app.document.pptx_parser import PPTXParser

        with patch.dict("sys.modules", {"pptx": None, "pptx.enum": None}):
            parser = PPTXParser()
            result = await parser.parse("/fake/path.pptx")
            assert result == ""

    @pytest.mark.asyncio
    async def test_parse_basic_slide(self) -> None:
        """解析基本幻灯片 — 文本提取。"""
        from app.document.pptx_parser import PPTXParser

        # 构建 mock slide — shapes 需要同时支持迭代和 .title 属性
        mock_text_frame = MagicMock()
        mock_text_frame.text = "这是幻灯片内容"

        mock_title = MagicMock()
        mock_title.text = "标题"

        mock_shape = MagicMock()
        mock_shape.has_table = False
        mock_shape.has_text_frame = True
        mock_shape.text_frame = mock_text_frame
        mock_shape.shape_type = MagicMock()
        mock_shape.shape_type.value = 1  # 非 PICTURE

        mock_shapes = MagicMock()
        mock_shapes.__iter__ = MagicMock(side_effect=lambda: iter([mock_shape]))
        mock_shapes.__len__ = MagicMock(return_value=1)
        mock_shapes.title = mock_title

        mock_slide = MagicMock()
        mock_slide.shapes = mock_shapes

        mock_prs = MagicMock()
        mock_prs.slides = [mock_slide]

        mock_pptx_module = MagicMock()
        mock_pptx_module.Presentation.return_value = mock_prs

        import sys
        with patch.dict(sys.modules, {
            "pptx": mock_pptx_module,
            "pptx.enum": MagicMock(),
            "pptx.enum.shapes": MagicMock(),
        }):
            parser = PPTXParser()
            with patch.object(parser, "_vlm_describe", AsyncMock(return_value="")):
                result = await parser.parse("/fake/doc.pptx")

        assert "<h2>标题</h2>" in result
        assert "这是幻灯片内容" in result

    @pytest.mark.asyncio
    async def test_parse_with_table(self) -> None:
        """解析包含表格的幻灯片。"""
        from app.document.pptx_parser import PPTXParser

        mock_cell1 = MagicMock()
        mock_cell1.text = "项目"
        mock_cell2 = MagicMock()
        mock_cell2.text = "状态"
        mock_cell3 = MagicMock()
        mock_cell3.text = "RAG"
        mock_cell4 = MagicMock()
        mock_cell4.text = "完成"

        mock_row1 = MagicMock()
        mock_row1.cells = [mock_cell1, mock_cell2]
        mock_row2 = MagicMock()
        mock_row2.cells = [mock_cell3, mock_cell4]

        mock_table = MagicMock()
        mock_table.rows = [mock_row1, mock_row2]

        mock_shape = MagicMock()
        mock_shape.has_table = True
        mock_shape.has_text_frame = False
        mock_shape.table = mock_table
        mock_shape.shape_type = MagicMock()
        mock_shape.shape_type.value = 1  # 非 PICTURE

        # 用 side_effect 使 shapes 可重复迭代
        mock_shapes = MagicMock()
        mock_shapes.__iter__ = MagicMock(side_effect=lambda: iter([mock_shape]))
        mock_shapes.__len__ = MagicMock(return_value=1)
        mock_shapes.title = None  # 无标题

        mock_slide = MagicMock()
        mock_slide.shapes = mock_shapes
        mock_slide.placeholders = MagicMock(return_value=iter([]))

        mock_prs = MagicMock()
        mock_prs.slides = [mock_slide]

        mock_pptx_module = MagicMock()
        mock_pptx_module.Presentation.return_value = mock_prs

        import sys
        with patch.dict(sys.modules, {
            "pptx": mock_pptx_module,
            "pptx.enum": MagicMock(),
            "pptx.enum.shapes": MagicMock(),
        }):
            parser = PPTXParser()
            result = await parser.parse("/fake/doc.pptx")

        assert "<table>" in result
        assert "<th>项目</th>" in result
        assert "<td>完成</td>" in result


# ======================================================================
# factory 测试
# ======================================================================


class TestFactory:
    """文档解析器工厂测试。"""

    def test_get_parser_pdf(self) -> None:
        from app.document.factory import get_parser
        from app.document.pdf_parser import PDFParser

        parser = get_parser("pdf")
        assert isinstance(parser, PDFParser)

    def test_get_parser_pptx(self) -> None:
        from app.document.factory import get_parser
        from app.document.pptx_parser import PPTXParser

        parser = get_parser("pptx")
        assert isinstance(parser, PPTXParser)

    def test_get_parser_ppt_alias(self) -> None:
        from app.document.factory import get_parser
        from app.document.pptx_parser import PPTXParser

        parser = get_parser("ppt")
        assert isinstance(parser, PPTXParser)

    def test_get_parser_unsupported(self) -> None:
        from app.document.factory import get_parser

        parser = get_parser("unknown")
        assert parser is None

    def test_get_parser_case_insensitive(self) -> None:
        from app.document.factory import get_parser
        from app.document.pdf_parser import PDFParser

        parser = get_parser("PDF")
        assert isinstance(parser, PDFParser)


# ======================================================================
# document_tasks 集成测试
# ======================================================================


class TestDocumentTasksParserIntegration:
    """document_tasks 解析器集成测试。"""

    @pytest.mark.asyncio
    async def test_parse_pdf_enhanced(self) -> None:
        """_parse_pdf 使用增强解析器。"""
        from tasks.document_tasks import _parse_pdf

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/doc.pdf"
        mock_doc.content_text = None

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="增强解析结果，包含<table>表格</table>")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_pdf(mock_doc)

        assert result == "增强解析结果，包含<table>表格</table>"

    @pytest.mark.asyncio
    async def test_parse_pdf_fallback_to_text(self) -> None:
        """增强解析器返回空时降级到纯文本。"""
        from tasks.document_tasks import _parse_pdf

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/doc.pdf"
        mock_doc.content_text = "fallback text"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="")  # 增强解析器返回空

        with patch("app.document.get_parser", return_value=mock_parser), \
             patch("tasks.document_tasks.fitz", create=True, side_effect=ImportError):
            result = await _parse_pdf(mock_doc)

        # 降级到 content_text
        assert result == "fallback text"

    @pytest.mark.asyncio
    async def test_parse_pptx(self) -> None:
        """_parse_pptx 使用增强解析器。"""
        from tasks.document_tasks import _parse_pptx

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/doc.pptx"
        mock_doc.content_text = None

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="<h2>幻灯片1</h2>\n内容")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_pptx(mock_doc)

        assert "<h2>幻灯片1</h2>" in result

    @pytest.mark.asyncio
    async def test_parse_pptx_no_file_path(self) -> None:
        """PPTX 无文件路径返回空字符串。"""
        from tasks.document_tasks import _parse_pptx

        mock_doc = MagicMock()
        mock_doc.file_path = None

        result = await _parse_pptx(mock_doc)
        assert result == ""

    @pytest.mark.asyncio
    async def test_parse_document_routes_pptx(self) -> None:
        """_parse_document 正确路由 pptx 类型。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "pptx"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="<h2>标题</h2>内容")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_document(mock_doc)

        assert "<h2>标题</h2>" in result


# ======================================================================
# config 配置项测试
# ======================================================================


class TestDocumentParserConfig:
    """文档解析配置项测试。"""

    def test_pdf_table_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "PDF_TABLE_EXTRACTION_ENABLED")
        assert settings.PDF_TABLE_EXTRACTION_ENABLED is True

    def test_pdf_image_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "PDF_IMAGE_EXTRACTION_ENABLED")
        assert settings.PDF_IMAGE_EXTRACTION_ENABLED is True
        assert hasattr(settings, "PDF_IMAGE_MAX_PER_DOC")
        assert settings.PDF_IMAGE_MAX_PER_DOC == 50

    def test_pptx_image_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "PPTX_IMAGE_EXTRACTION_ENABLED")
        assert settings.PPTX_IMAGE_EXTRACTION_ENABLED is True
        assert hasattr(settings, "PPTX_IMAGE_MAX_PER_DOC")
        assert settings.PPTX_IMAGE_MAX_PER_DOC == 50
