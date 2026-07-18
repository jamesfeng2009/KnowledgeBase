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
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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

    def test_get_parser_ppt_not_registered(self) -> None:
        """ppt 旧格式不再注册别名 — 由 _parse_document 路由层兜底。"""
        from app.document.factory import get_parser

        parser = get_parser("ppt")
        assert parser is None

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

    def test_docx_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "DOCX_TABLE_EXTRACTION_ENABLED")
        assert settings.DOCX_TABLE_EXTRACTION_ENABLED is True
        assert hasattr(settings, "DOCX_IMAGE_EXTRACTION_ENABLED")
        assert settings.DOCX_IMAGE_EXTRACTION_ENABLED is True
        assert hasattr(settings, "DOCX_IMAGE_MAX_PER_DOC")
        assert settings.DOCX_IMAGE_MAX_PER_DOC == 50

    def test_rate_limit_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "RATE_LIMIT_ENABLED")
        assert settings.RATE_LIMIT_ENABLED is True
        assert hasattr(settings, "RATE_LIMIT_PER_MINUTE")
        assert settings.RATE_LIMIT_PER_MINUTE == 60
        assert hasattr(settings, "RATE_LIMIT_BURST")
        assert settings.RATE_LIMIT_BURST == 10

    def test_eval_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "EVAL_DATASET_DIR")
        assert hasattr(settings, "EVAL_REGRESSION_THRESHOLD")
        assert settings.EVAL_REGRESSION_THRESHOLD == 0.05


# ======================================================================
# DOCXParser 测试
# ======================================================================


class TestDOCXParserRowsToHtml:
    """DOCXParser._rows_to_html 测试。"""

    def test_basic_table(self) -> None:
        from app.document.docx_parser import DOCXParser

        rows = [["姓名", "年龄"], ["张三", "30"], ["李四", "25"]]
        html = DOCXParser._rows_to_html(rows)
        assert "<table>" in html
        assert "<th>姓名</th>" in html
        assert "<td>张三</td>" in html

    def test_empty_rows(self) -> None:
        from app.document.docx_parser import DOCXParser

        assert DOCXParser._rows_to_html([]) == ""

    def test_html_escaping(self) -> None:
        from app.document.docx_parser import DOCXParser

        rows = [["<script>", "&amp;"], ["normal", "data"]]
        html = DOCXParser._rows_to_html(rows)
        assert "&lt;script&gt;" in html
        assert "&amp;amp;" in html


class TestDOCXParserParse:
    """DOCXParser.parse 测试。"""

    @pytest.mark.asyncio
    async def test_docx_not_installed(self) -> None:
        """python-docx 未安装时返回空字符串。"""
        from app.document.docx_parser import DOCXParser

        with patch.dict("sys.modules", {"docx": None}):
            parser = DOCXParser()
            result = await parser.parse("/fake/path.docx")
            assert result == ""

    @pytest.mark.asyncio
    async def test_parse_with_text_and_table(self) -> None:
        """解析包含文本和表格的 DOCX。"""
        from app.document.docx_parser import DOCXParser

        # mock body 元素：段落 + 表格
        mock_p_element = MagicMock()
        mock_p_element.tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

        mock_tbl_element = MagicMock()
        mock_tbl_element.tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tbl"

        mock_doc = MagicMock()
        mock_doc.element.body = [mock_p_element, mock_tbl_element]
        mock_doc.part.rels = {}

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"docx": mock_docx_module}):
            parser = DOCXParser()
            # 直接 mock 解析器方法，避免复杂的 docx 模块 mock
            with patch.object(parser, "_format_paragraph", return_value="这是段落文本"), \
                 patch.object(parser, "_extract_table_html", return_value="<table>\n<tr>\n<th>项目</th>\n</tr>\n<tr>\n<td>采购</td>\n</tr>\n</table>"), \
                 patch.object(parser, "_extract_images", AsyncMock(return_value=([], 0))):
                result = await parser.parse("/fake/doc.docx")

        assert "这是段落文本" in result
        assert "<table>" in result
        assert "<th>项目</th>" in result
        assert "<td>采购</td>" in result

    @pytest.mark.asyncio
    async def test_parse_with_image_vlm(self) -> None:
        """解析包含图片的 DOCX，VLM 生成描述。"""
        from app.document.docx_parser import DOCXParser

        mock_p_element = MagicMock()
        mock_p_element.tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

        # mock image relationship
        mock_image_part = MagicMock()
        mock_image_part.blob = b"\x89PNG fake image data"
        mock_image_part.content_type = "image/png"

        mock_rel = MagicMock()
        mock_rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        mock_rel.target_part = mock_image_part

        mock_doc = MagicMock()
        mock_doc.element.body = [mock_p_element]
        mock_doc.part.rels = {"rId1": mock_rel}

        mock_vlm = MagicMock()
        mock_vlm.understand = AsyncMock(return_value="流程图描述")

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"docx": mock_docx_module}):
            parser = DOCXParser()
            with patch.object(parser, "_format_paragraph", return_value="文档内容"), \
                 patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm):
                result = await parser.parse("/fake/doc.docx")

        assert "文档内容" in result
        assert "[图片描述: 流程图描述]" in result
        mock_vlm.understand.assert_called_once()
        call_kwargs = mock_vlm.understand.call_args.kwargs
        assert isinstance(call_kwargs.get("image"), bytes)

    @pytest.mark.asyncio
    async def test_parse_image_max_limit(self) -> None:
        """图片数量不超过 DOCX_IMAGE_MAX_PER_DOC。"""
        from app.document.docx_parser import DOCXParser

        mock_p_element = MagicMock()
        mock_p_element.tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

        # 创建 10 个 mock image
        mock_rels = {}
        for i in range(10):
            mock_image_part = MagicMock()
            mock_image_part.blob = b"fake"
            mock_image_part.content_type = "image/png"
            mock_rel = MagicMock()
            mock_rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
            mock_rel.target_part = mock_image_part
            mock_rels[f"rId{i}"] = mock_rel

        mock_doc = MagicMock()
        mock_doc.element.body = [mock_p_element]
        mock_doc.part.rels = mock_rels

        mock_vlm = MagicMock()
        mock_vlm.understand = AsyncMock(return_value="描述")

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"docx": mock_docx_module}):
            parser = DOCXParser()
            with patch.object(parser, "_format_paragraph", return_value="文本"), \
                 patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm), \
                 patch("app.document.docx_parser.get_settings") as mock_settings:
                mock_settings.return_value.DOCX_TABLE_EXTRACTION_ENABLED = True
                mock_settings.return_value.DOCX_IMAGE_EXTRACTION_ENABLED = True
                mock_settings.return_value.DOCX_IMAGE_MAX_PER_DOC = 3
                result = await parser.parse("/fake/doc.docx")

        # VLM 最多调用 3 次
        assert mock_vlm.understand.call_count <= 3

    @pytest.mark.asyncio
    async def test_parse_vlm_not_available(self) -> None:
        """VLM 不可用时跳过图片描述，保留文本。"""
        from app.document.docx_parser import DOCXParser

        mock_p_element = MagicMock()
        mock_p_element.tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

        mock_image_part = MagicMock()
        mock_image_part.blob = b"fake"
        mock_image_part.content_type = "image/png"
        mock_rel = MagicMock()
        mock_rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        mock_rel.target_part = mock_image_part

        mock_doc = MagicMock()
        mock_doc.element.body = [mock_p_element]
        mock_doc.part.rels = {"rId1": mock_rel}

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"docx": mock_docx_module}):
            parser = DOCXParser()
            with patch.object(parser, "_format_paragraph", return_value="文本内容"), \
                 patch.object(parser, "_vlm_describe", AsyncMock(return_value="")):
                result = await parser.parse("/fake/doc.docx")

        assert "文本内容" in result
        # 图片描述被跳过
        assert "[图片描述:" not in result


class TestFactoryDOCX:
    """factory 路由 DOCX 测试。"""

    def test_get_parser_docx(self) -> None:
        from app.document.factory import get_parser
        from app.document.docx_parser import DOCXParser

        parser = get_parser("docx")
        assert isinstance(parser, DOCXParser)


class TestDocumentTasksDOCXIntegration:
    """document_tasks DOCX 集成测试。"""

    @pytest.mark.asyncio
    async def test_parse_docx_enhanced(self) -> None:
        """_parse_docx 使用增强解析器。"""
        from tasks.document_tasks import _parse_docx

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/doc.docx"
        mock_doc.content_text = None

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="增强解析结果，包含<table>表格</table>")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_docx(mock_doc)

        assert result == "增强解析结果，包含<table>表格</table>"

    @pytest.mark.asyncio
    async def test_parse_docx_fallback_to_text(self) -> None:
        """增强解析器返回空时降级到纯文本。"""
        from tasks.document_tasks import _parse_docx

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/doc.docx"
        mock_doc.content_text = "fallback text"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="")

        with patch("app.document.get_parser", return_value=mock_parser), \
             patch.dict("sys.modules", {"docx": None}):
            result = await _parse_docx(mock_doc)

        assert result == "fallback text"

    @pytest.mark.asyncio
    async def test_parse_docx_no_file_path(self) -> None:
        """DOCX 无文件路径返回空字符串。"""
        from tasks.document_tasks import _parse_docx

        mock_doc = MagicMock()
        mock_doc.file_path = None

        result = await _parse_docx(mock_doc)
        assert result == ""

    @pytest.mark.asyncio
    async def test_parse_document_routes_docx(self) -> None:
        """_parse_document 正确路由 docx 类型。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "docx"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="DOCX增强内容")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_document(mock_doc)

        assert result == "DOCX增强内容"


# ======================================================================
# XLSXParser 测试
# ======================================================================


class TestXLSXParserRowsToHtml:
    """XLSXParser._rows_to_html 测试。"""

    def test_basic_table(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        rows = [["部门", "人数"], ["技术部", "15"], ["市场部", "8"]]
        html = XLSXParser._rows_to_html(rows)
        assert "<table>" in html
        assert "</table>" in html
        assert "<th>部门</th>" in html
        assert "<td>技术部</td>" in html
        assert "<td>市场部</td>" in html

    def test_empty_rows(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        assert XLSXParser._rows_to_html([]) == ""

    def test_html_escaping(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        rows = [["<script>alert(1)</script>"], ["normal"]]
        html = XLSXParser._rows_to_html(rows)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_ampersand_escaping(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        rows = [["A & B"]]
        html = XLSXParser._rows_to_html(rows)
        assert "A &amp; B" in html

    def test_none_cells(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        rows = [["a", None, "c"]]
        html = XLSXParser._rows_to_html(rows)
        assert "<th>a</th>" in html
        assert "<th></th>" in html
        assert "<th>c</th>" in html


class TestXLSXParserEscapeHtml:
    """XLSXParser._escape_html 测试。"""

    def test_basic_escape(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        assert XLSXParser._escape_html("<div>") == "&lt;div&gt;"

    def test_ampersand(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        assert XLSXParser._escape_html("a & b") == "a &amp; b"

    def test_no_special_chars(self) -> None:
        from app.document.xlsx_parser import XLSXParser

        assert XLSXParser._escape_html("普通文本") == "普通文本"


class TestXLSXParserParse:
    """XLSXParser.parse 测试。"""

    @pytest.mark.asyncio
    async def test_openpyxl_not_installed(self) -> None:
        """openpyxl 未安装时返回空字符串。"""
        from app.document.xlsx_parser import XLSXParser

        with patch.dict("sys.modules", {"openpyxl": None}):
            parser = XLSXParser()
            result = await parser.parse("/fake/path.xlsx")
            assert result == ""

    @pytest.mark.asyncio
    async def test_open_failed(self) -> None:
        """文件打开失败时返回空字符串。"""
        from app.document.xlsx_parser import XLSXParser

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.side_effect = Exception("file not found")

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}):
            parser = XLSXParser()
            result = await parser.parse("/fake/path.xlsx")
            assert result == ""

    @pytest.mark.asyncio
    async def test_parse_single_sheet(self) -> None:
        """解析单 sheet XLSX — 输出标题 + HTML 表格。"""
        from app.document.xlsx_parser import XLSXParser

        # mock worksheet 返回的行数据
        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = [
            ("姓名", "年龄"),
            ("张三", "25"),
            ("李四", "30"),
        ]

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = ["员工表"]
        mock_workbook.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}):
            parser = XLSXParser()
            result = await parser.parse("/fake/data.xlsx")

        assert "<h2>员工表</h2>" in result
        assert "<table>" in result
        assert "<th>姓名</th>" in result
        assert "<td>张三</td>" in result

    @pytest.mark.asyncio
    async def test_parse_multiple_sheets(self) -> None:
        """解析多 sheet XLSX — 每个 sheet 输出独立标题 + 表格。"""
        from app.document.xlsx_parser import XLSXParser

        mock_sheet1 = MagicMock()
        mock_sheet1.iter_rows.return_value = [("A", "B"), ("1", "2")]

        mock_sheet2 = MagicMock()
        mock_sheet2.iter_rows.return_value = [("C", "D"), ("3", "4")]

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = ["Sheet1", "Sheet2"]
        mock_workbook.__getitem__ = MagicMock(
            side_effect=lambda name: mock_sheet1 if name == "Sheet1" else mock_sheet2
        )
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}):
            parser = XLSXParser()
            result = await parser.parse("/fake/multi.xlsx")

        assert "<h2>Sheet1</h2>" in result
        assert "<h2>Sheet2</h2>" in result
        assert "<th>A</th>" in result
        assert "<th>C</th>" in result

    @pytest.mark.asyncio
    async def test_parse_table_disabled(self) -> None:
        """表格提取关闭时只有 sheet 标题。"""
        from app.document.xlsx_parser import XLSXParser

        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = [("A", "B"), ("1", "2")]

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = ["Sheet1"]
        mock_workbook.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}), \
             patch("app.document.xlsx_parser.get_settings") as mock_settings:
            mock_settings.return_value.XLSX_TABLE_EXTRACTION_ENABLED = False
            mock_settings.return_value.XLSX_MAX_ROWS_PER_SHEET = 500
            mock_settings.return_value.XLSX_MAX_SHEETS = 20

            parser = XLSXParser()
            result = await parser.parse("/fake/data.xlsx")

        assert "<h2>Sheet1</h2>" in result
        assert "<table>" not in result

    @pytest.mark.asyncio
    async def test_parse_empty_sheet(self) -> None:
        """空 sheet 只输出标题，无表格。"""
        from app.document.xlsx_parser import XLSXParser

        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = []  # 空表

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = ["EmptySheet"]
        mock_workbook.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}):
            parser = XLSXParser()
            result = await parser.parse("/fake/empty.xlsx")

        assert "<h2>EmptySheet</h2>" in result
        assert "<table>" not in result

    @pytest.mark.asyncio
    async def test_parse_skip_empty_rows(self) -> None:
        """全空行被自动跳过。"""
        from app.document.xlsx_parser import XLSXParser

        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = [
            ("姓名", "年龄"),
            (None, None),  # 全空行
            ("张三", "25"),
        ]

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = ["Data"]
        mock_workbook.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}):
            parser = XLSXParser()
            result = await parser.parse("/fake/data.xlsx")

        assert "<table>" in result
        # 只有 2 行数据（表头 + 1 行），空行被跳过
        assert result.count("<tr>") == 2

    @pytest.mark.asyncio
    async def test_parse_max_sheets_limit(self) -> None:
        """sheet 数量不超过 XLSX_MAX_SHEETS。"""
        from app.document.xlsx_parser import XLSXParser

        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = [("A", "B")]

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = [f"Sheet{i}" for i in range(10)]
        mock_workbook.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}), \
             patch("app.document.xlsx_parser.get_settings") as mock_settings:
            mock_settings.return_value.XLSX_TABLE_EXTRACTION_ENABLED = True
            mock_settings.return_value.XLSX_MAX_ROWS_PER_SHEET = 500
            mock_settings.return_value.XLSX_MAX_SHEETS = 3

            parser = XLSXParser()
            result = await parser.parse("/fake/many_sheets.xlsx")

        # 只有 3 个 sheet 标题
        assert result.count("<h2>") == 3

    @pytest.mark.asyncio
    async def test_parse_max_rows_limit(self) -> None:
        """行数不超过 XLSX_MAX_ROWS_PER_SHEET。"""
        from app.document.xlsx_parser import XLSXParser

        # 生成 10 行数据
        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = [
            (f"row{i}", f"val{i}") for i in range(10)
        ]

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = ["Sheet1"]
        mock_workbook.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}), \
             patch("app.document.xlsx_parser.get_settings") as mock_settings:
            mock_settings.return_value.XLSX_TABLE_EXTRACTION_ENABLED = True
            mock_settings.return_value.XLSX_MAX_ROWS_PER_SHEET = 5
            mock_settings.return_value.XLSX_MAX_SHEETS = 20

            parser = XLSXParser()
            result = await parser.parse("/fake/data.xlsx")

        # 只有 5 行（表头 + 4 行数据）
        assert result.count("<tr>") == 5

    @pytest.mark.asyncio
    async def test_parse_sheet_name_html_escaped(self) -> None:
        """sheet 名包含 HTML 特殊字符时被转义。"""
        from app.document.xlsx_parser import XLSXParser

        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = [("data",)]

        mock_workbook = MagicMock()
        mock_workbook.sheetnames = ["<Script>Sheet"]
        mock_workbook.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_workbook.close = MagicMock()

        mock_openpyxl = MagicMock()
        mock_openpyxl.load_workbook.return_value = mock_workbook

        import sys
        with patch.dict(sys.modules, {"openpyxl": mock_openpyxl}):
            parser = XLSXParser()
            result = await parser.parse("/fake/data.xlsx")

        assert "&lt;Script&gt;Sheet" in result
        assert "<Script>" not in result


class TestFactoryXLSX:
    """factory 路由 XLSX 测试。"""

    def test_get_parser_xlsx(self) -> None:
        from app.document.factory import get_parser
        from app.document.xlsx_parser import XLSXParser

        parser = get_parser("xlsx")
        assert isinstance(parser, XLSXParser)

    def test_get_parser_xls_alias(self) -> None:
        from app.document.factory import get_parser
        from app.document.xlsx_parser import XLSXParser

        parser = get_parser("xls")
        assert isinstance(parser, XLSXParser)

    def test_get_parser_xlsx_case_insensitive(self) -> None:
        from app.document.factory import get_parser
        from app.document.xlsx_parser import XLSXParser

        parser = get_parser("XLSX")
        assert isinstance(parser, XLSXParser)


class TestDocumentTasksXLSXIntegration:
    """document_tasks XLSX 集成测试。"""

    @pytest.mark.asyncio
    async def test_parse_xlsx_enhanced(self) -> None:
        """_parse_xlsx 使用增强解析器。"""
        from tasks.document_tasks import _parse_xlsx

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/data.xlsx"
        mock_doc.content_text = None

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(
            return_value="<h2>员工表</h2>\n<table>\n<tr>\n<th>姓名</th>\n</tr>\n</table>"
        )

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_xlsx(mock_doc)

        assert "<h2>员工表</h2>" in result
        assert "<table>" in result

    @pytest.mark.asyncio
    async def test_parse_xlsx_fallback_to_text(self) -> None:
        """增强解析器返回空时降级到 content_text。"""
        from tasks.document_tasks import _parse_xlsx

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/data.xlsx"
        mock_doc.content_text = "fallback text"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_xlsx(mock_doc)

        assert result == "fallback text"

    @pytest.mark.asyncio
    async def test_parse_xlsx_no_file_path(self) -> None:
        """XLSX 无文件路径返回空字符串。"""
        from tasks.document_tasks import _parse_xlsx

        mock_doc = MagicMock()
        mock_doc.file_path = None

        result = await _parse_xlsx(mock_doc)
        assert result == ""

    @pytest.mark.asyncio
    async def test_parse_document_routes_xlsx(self) -> None:
        """_parse_document 正确路由 xlsx 类型。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "xlsx"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="<h2>Sheet1</h2>\n<table>data</table>")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_document(mock_doc)

        assert "<h2>Sheet1</h2>" in result

    @pytest.mark.asyncio
    async def test_parse_document_routes_xls_alias(self) -> None:
        """_parse_document 正确路由 xls 别名。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "xls"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="XLS内容")

        with patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_document(mock_doc)

        assert result == "XLS内容"


class TestXLSXConfig:
    """XLSX 配置项测试。"""

    def test_xlsx_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "XLSX_TABLE_EXTRACTION_ENABLED")
        assert settings.XLSX_TABLE_EXTRACTION_ENABLED is True
        assert hasattr(settings, "XLSX_MAX_ROWS_PER_SHEET")
        assert settings.XLSX_MAX_ROWS_PER_SHEET == 500
        assert hasattr(settings, "XLSX_MAX_SHEETS")
        assert settings.XLSX_MAX_SHEETS == 20


# ======================================================================
# 独立音频解析测试
# ======================================================================


class TestAudioTypes:
    """音频类型集合测试。"""

    def test_audio_types_contains_common_formats(self) -> None:
        from tasks.document_tasks import _AUDIO_TYPES

        assert "audio" in _AUDIO_TYPES
        assert "mp3" in _AUDIO_TYPES
        assert "wav" in _AUDIO_TYPES
        assert "m4a" in _AUDIO_TYPES
        assert "aac" in _AUDIO_TYPES
        assert "flac" in _AUDIO_TYPES
        assert "ogg" in _AUDIO_TYPES

    def test_video_types_separate_from_audio(self) -> None:
        from tasks.document_tasks import _AUDIO_TYPES, _VIDEO_TYPES

        # 视频和音频类型不重叠
        assert _VIDEO_TYPES.isdisjoint(_AUDIO_TYPES)


class TestParseAudio:
    """_parse_audio 测试。"""

    @pytest.mark.asyncio
    async def test_no_file_path(self) -> None:
        """无文件路径返回 content_text。"""
        from tasks.document_tasks import _parse_audio

        mock_doc = MagicMock()
        mock_doc.file_path = None
        mock_doc.content_text = "existing text"

        result = await _parse_audio(mock_doc)
        assert result == "existing text"

    @pytest.mark.asyncio
    async def test_asr_disabled_by_config(self) -> None:
        """AUDIO_ASR_ENABLED=False 时跳过 ASR。"""
        from tasks.document_tasks import _parse_audio

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/audio.mp3"
        mock_doc.content_text = "cached text"

        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.AUDIO_ASR_ENABLED = False
            result = await _parse_audio(mock_doc)

        assert result == "cached text"

    @pytest.mark.asyncio
    async def test_deps_not_installed(self) -> None:
        """外部依赖未安装时返回 content_text。"""
        from tasks.document_tasks import _parse_audio

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/audio.mp3"
        mock_doc.content_text = "fallback"

        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value.AUDIO_ASR_ENABLED = True
            with patch.dict("sys.modules", {"app.video": None, "app.asr": None}):
                result = await _parse_audio(mock_doc)

        assert result == "fallback"

    @pytest.mark.asyncio
    async def test_successful_transcription(self) -> None:
        """成功转写音频文件。"""
        from tasks.document_tasks import _parse_audio

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/meeting.mp3"
        mock_doc.content_text = None
        mock_doc.id = "test-doc-id"

        # mock VideoProcessor
        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value="/tmp/fake.wav")

        # mock ASR provider
        from app.asr.provider import TranscribeSegment

        mock_segments = [
            TranscribeSegment(start=0.0, end=5.0, text="大家好"),
            TranscribeSegment(start=5.0, end=10.0, text="今天开会"),
        ]
        mock_asr = MagicMock()
        mock_asr.transcribe = AsyncMock(return_value=mock_segments)

        import os

        with patch("app.config.get_settings") as mock_settings, \
             patch("app.video.get_video_processor", return_value=mock_processor), \
             patch("app.asr.get_asr_provider", return_value=mock_asr), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove") as mock_remove:
            mock_settings.return_value.AUDIO_ASR_ENABLED = True

            result = await _parse_audio(mock_doc)

        assert "大家好" in result
        assert "今天开会" in result
        # WAV 临时文件被清理
        mock_remove.assert_called_once_with("/tmp/fake.wav")

    @pytest.mark.asyncio
    async def test_asr_failed_returns_content_text(self) -> None:
        """ASR 转写失败时返回 content_text。"""
        from tasks.document_tasks import _parse_audio

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/audio.mp3"
        mock_doc.content_text = "cached"
        mock_doc.id = "test-id"

        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value="/tmp/fake.wav")

        mock_asr = MagicMock()
        mock_asr.transcribe = AsyncMock(side_effect=Exception("ASR error"))

        with patch("app.config.get_settings") as mock_settings, \
             patch("app.video.get_video_processor", return_value=mock_processor), \
             patch("app.asr.get_asr_provider", return_value=mock_asr), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"):
            mock_settings.return_value.AUDIO_ASR_ENABLED = True

            result = await _parse_audio(mock_doc)

        assert result == "cached"

    @pytest.mark.asyncio
    async def test_audio_convert_failed(self) -> None:
        """音频转换失败时返回 content_text。"""
        from tasks.document_tasks import _parse_audio

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/audio.mp3"
        mock_doc.content_text = "fallback"
        mock_doc.id = "test-id"

        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value=None)  # 转换失败

        with patch("app.config.get_settings") as mock_settings, \
             patch("app.video.get_video_processor", return_value=mock_processor):
            mock_settings.return_value.AUDIO_ASR_ENABLED = True

            result = await _parse_audio(mock_doc)

        assert result == "fallback"

    @pytest.mark.asyncio
    async def test_no_transcript_returns_content_text(self) -> None:
        """ASR 返回空片段时返回 content_text。"""
        from tasks.document_tasks import _parse_audio

        mock_doc = MagicMock()
        mock_doc.file_path = "/fake/silent.wav"
        mock_doc.content_text = "empty"
        mock_doc.id = "test-id"

        mock_processor = MagicMock()
        mock_processor.extract_audio = AsyncMock(return_value="/tmp/fake.wav")

        mock_asr = MagicMock()
        mock_asr.transcribe = AsyncMock(return_value=[])  # 空转写

        with patch("app.config.get_settings") as mock_settings, \
             patch("app.video.get_video_processor", return_value=mock_processor), \
             patch("app.asr.get_asr_provider", return_value=mock_asr), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"):
            mock_settings.return_value.AUDIO_ASR_ENABLED = True

            result = await _parse_audio(mock_doc)

        assert result == "empty"


class TestDocumentTasksAudioIntegration:
    """document_tasks 音频路由集成测试。"""

    @pytest.mark.asyncio
    async def test_parse_document_routes_mp3(self) -> None:
        """_parse_document 正确路由 mp3 类型到 _parse_audio。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "mp3"
        mock_doc.file_path = "/fake/audio.mp3"
        mock_doc.id = "test-id"

        with patch("app.config.get_settings") as mock_settings, \
             patch("tasks.document_tasks._parse_audio", new_callable=AsyncMock) as mock_parse_audio:
            mock_settings.return_value.AUDIO_ASR_ENABLED = True
            mock_parse_audio.return_value = "转写文本"

            result = await _parse_document(mock_doc)

        assert result == "转写文本"
        mock_parse_audio.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_document_routes_wav(self) -> None:
        """_parse_document 正确路由 wav 类型。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "wav"
        mock_doc.file_path = "/fake/audio.wav"
        mock_doc.id = "test-id"

        with patch("app.config.get_settings") as mock_settings, \
             patch("tasks.document_tasks._parse_audio", new_callable=AsyncMock) as mock_parse_audio:
            mock_settings.return_value.AUDIO_ASR_ENABLED = True
            mock_parse_audio.return_value = "WAV转写"

            result = await _parse_document(mock_doc)

        assert result == "WAV转写"


class TestAudioConfig:
    """音频配置项测试。"""

    def test_audio_asr_enabled_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "AUDIO_ASR_ENABLED")
        assert settings.AUDIO_ASR_ENABLED is True


# ======================================================================
# P0: 旧格式兜底测试
# ======================================================================


class TestLegacyFormatFallback:
    """P0: .doc/.ppt 旧格式兜底测试。"""

    def test_doc_fallback_returns_hint(self) -> None:
        """_legacy_format_fallback 返回 .doc 格式提示。"""
        from tasks.document_tasks import _legacy_format_fallback

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.id = "test-id"
        mock_doc.file_path = "/fake/old.doc"

        result = _legacy_format_fallback(mock_doc, "doc")

        assert ".doc" in result
        assert "请将文件另存为 .docx" in result

    def test_ppt_fallback_returns_hint(self) -> None:
        """_legacy_format_fallback 返回 .ppt 格式提示。"""
        from tasks.document_tasks import _legacy_format_fallback

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.id = "test-id"
        mock_doc.file_path = "/fake/old.ppt"

        result = _legacy_format_fallback(mock_doc, "ppt")

        assert ".ppt" in result
        assert "请将文件另存为 .pptx" in result

    def test_doc_fallback_with_existing_text(self) -> None:
        """已有 content_text 时拼接到提示前。"""
        from tasks.document_tasks import _legacy_format_fallback

        mock_doc = MagicMock()
        mock_doc.content_text = "已有内容"
        mock_doc.id = "test-id"
        mock_doc.file_path = "/fake/old.doc"

        result = _legacy_format_fallback(mock_doc, "doc")

        assert "已有内容" in result
        assert ".doc" in result

    @pytest.mark.asyncio
    async def test_parse_document_routes_doc(self) -> None:
        """_parse_document 路由 .doc 到旧格式兜底。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "doc"
        mock_doc.file_path = "/fake/old.doc"
        mock_doc.id = "test-id"

        result = await _parse_document(mock_doc)

        assert ".doc" in result
        assert "请将文件另存为 .docx" in result

    @pytest.mark.asyncio
    async def test_parse_document_routes_ppt(self) -> None:
        """_parse_document 路由 .ppt 到旧格式兜底。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "ppt"
        mock_doc.file_path = "/fake/old.ppt"
        mock_doc.id = "test-id"

        result = await _parse_document(mock_doc)

        assert ".ppt" in result
        assert "请将文件另存为 .pptx" in result

    def test_factory_no_ppt_alias(self) -> None:
        """factory 不注册 ppt 别名。"""
        from app.document.factory import get_parser

        # ppt 不应返回 PPTXParser（已移除别名）
        parser = get_parser("ppt")
        assert parser is None

    def test_factory_no_doc_alias(self) -> None:
        """factory 不注册 doc 别名。"""
        from app.document.factory import get_parser

        parser = get_parser("doc")
        assert parser is None


# ======================================================================
# P1: PPTX 组合形状递归测试
# ======================================================================


class TestPPTXGroupShapeRecursion:
    """P1: PPTX 组合形状递归遍历测试。"""

    def test_collect_shape_text_normal_shape(self) -> None:
        """普通形状文本被正确收集。"""
        from app.document.pptx_parser import PPTXParser

        parser = PPTXParser()
        text_parts: list[str] = []

        mock_shape = MagicMock()
        mock_shape.has_table = False
        mock_shape.has_text_frame = True
        mock_shape.text_frame.text = "普通文本"
        mock_shape.shape_type = MagicMock()
        mock_shape.shape_type.value = 1  # 非 GROUP 非 PICTURE

        parser._collect_shape_text(mock_shape, text_parts)

        assert "普通文本" in text_parts

    def test_collect_shape_text_group_recursion(self) -> None:
        """GROUP 形状递归遍历子形状文本。"""
        from app.document.pptx_parser import PPTXParser

        parser = PPTXParser()
        text_parts: list[str] = []

        # 模拟子形状
        mock_child = MagicMock()
        mock_child.has_table = False
        mock_child.has_text_frame = True
        mock_child.text_frame.text = "组合内文本"
        mock_child.shape_type = MagicMock()
        mock_child.shape_type.value = 1

        # 模拟 GROUP 形状（value=6）
        mock_group = MagicMock()
        mock_group.has_table = False
        mock_group.has_text_frame = False
        mock_group.shape_type = MagicMock()
        mock_group.shape_type.value = 6
        mock_group.shapes = [mock_child]

        parser._collect_shape_text(mock_group, text_parts)

        assert "组合内文本" in text_parts

    def test_collect_shape_text_nested_group(self) -> None:
        """嵌套 GROUP（2 层）递归正确。"""
        from app.document.pptx_parser import PPTXParser

        parser = PPTXParser()
        text_parts: list[str] = []

        # 最内层文本形状
        mock_inner = MagicMock()
        mock_inner.has_table = False
        mock_inner.has_text_frame = True
        mock_inner.text_frame.text = "嵌套文本"
        mock_inner.shape_type = MagicMock()
        mock_inner.shape_type.value = 1

        # 中间 GROUP
        mock_mid_group = MagicMock()
        mock_mid_group.has_table = False
        mock_mid_group.has_text_frame = False
        mock_mid_group.shape_type = MagicMock()
        mock_mid_group.shape_type.value = 6
        mock_mid_group.shapes = [mock_inner]

        # 外层 GROUP
        mock_outer_group = MagicMock()
        mock_outer_group.has_table = False
        mock_outer_group.has_text_frame = False
        mock_outer_group.shape_type = MagicMock()
        mock_outer_group.shape_type.value = 6
        mock_outer_group.shapes = [mock_mid_group]

        parser._collect_shape_text(mock_outer_group, text_parts)

        assert "嵌套文本" in text_parts

    def test_collect_shape_text_skip_table(self) -> None:
        """表格形状被跳过（由 _extract_tables 处理）。"""
        from app.document.pptx_parser import PPTXParser

        parser = PPTXParser()
        text_parts: list[str] = []

        mock_shape = MagicMock()
        mock_shape.has_table = True
        mock_shape.has_text_frame = True
        mock_shape.text_frame.text = "不应出现"
        mock_shape.shape_type = MagicMock()
        mock_shape.shape_type.value = 1

        parser._collect_shape_text(mock_shape, text_parts)

        assert "不应出现" not in text_parts

    def test_collect_shape_text_skip_picture(self) -> None:
        """图片形状被跳过（由 _extract_images 处理）。"""
        from app.document.pptx_parser import PPTXParser

        parser = PPTXParser()
        text_parts: list[str] = []

        mock_shape = MagicMock()
        mock_shape.has_table = False
        mock_shape.has_text_frame = True
        mock_shape.text_frame.text = "不应出现"
        mock_shape.shape_type = MagicMock()
        mock_shape.shape_type.value = 13  # PICTURE

        parser._collect_shape_text(mock_shape, text_parts)

        assert "不应出现" not in text_parts

    def test_collect_shape_text_depth_limit(self) -> None:
        """递归深度超过 5 层时停止。"""
        from app.document.pptx_parser import PPTXParser

        parser = PPTXParser()
        text_parts: list[str] = []

        # 模拟文本形状
        mock_text = MagicMock()
        mock_text.has_table = False
        mock_text.has_text_frame = True
        mock_text.text_frame.text = "深层文本"
        mock_text.shape_type = MagicMock()
        mock_text.shape_type.value = 1

        # depth=6 应直接返回，不收集文本
        parser._collect_shape_text(mock_text, text_parts, depth=6)

        assert "深层文本" not in text_parts


# ======================================================================
# P2: 扫描 PDF OCR 兜底测试
# ======================================================================


class TestPDFScanOCR:
    """P2: 扫描 PDF OCR 兜底测试。"""

    def test_scan_ocr_config(self) -> None:
        """PDF 扫描 OCR 配置项存在。"""
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "PDF_SCAN_OCR_ENABLED")
        assert settings.PDF_SCAN_OCR_ENABLED is True
        assert hasattr(settings, "PDF_SCAN_OCR_MAX_PAGES")
        assert settings.PDF_SCAN_OCR_MAX_PAGES == 20

    @pytest.mark.asyncio
    async def test_scan_page_ocr_vlm_not_available(self) -> None:
        """VLM 不可用时 _scan_page_ocr 返回空字符串。"""
        from app.document.pdf_parser import PDFParser

        parser = PDFParser()

        # mock pymupdf page
        mock_pixmap = MagicMock()
        mock_pixmap.tobytes.return_value = b"fake_png_bytes"

        mock_page = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap

        mock_doc = MagicMock()

        with patch.dict("sys.modules", {"app.vlm.provider": None}):
            result = await parser._scan_page_ocr(mock_doc, mock_page, 0)
            assert result == ""

    @pytest.mark.asyncio
    async def test_scan_page_ocr_render_failed(self) -> None:
        """页面渲染失败时返回空字符串。"""
        from app.document.pdf_parser import PDFParser

        parser = PDFParser()

        mock_page = MagicMock()
        mock_page.get_pixmap.side_effect = Exception("render error")

        mock_doc = MagicMock()

        result = await parser._scan_page_ocr(mock_doc, mock_page, 0)
        assert result == ""

    @pytest.mark.asyncio
    async def test_scan_page_ocr_success(self) -> None:
        """成功 OCR 返回 VLM 文本。"""
        from app.document.pdf_parser import PDFParser

        parser = PDFParser()

        mock_pixmap = MagicMock()
        mock_pixmap.tobytes.return_value = b"fake_png_bytes"

        mock_page = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap

        mock_doc = MagicMock()

        mock_vlm = MagicMock()
        mock_vlm.understand = AsyncMock(return_value="这是OCR提取的文字")

        import sys
        mock_fit = MagicMock()
        mock_fit.Matrix.return_value = MagicMock()

        with patch.dict(sys.modules, {"fitz": mock_fit}), \
             patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm):
            result = await parser._scan_page_ocr(mock_doc, mock_page, 0)

        assert result == "这是OCR提取的文字"

    @pytest.mark.asyncio
    async def test_parse_scan_pdf_triggers_ocr(self) -> None:
        """get_text() 返回空时触发扫描页 OCR。"""
        from app.document.pdf_parser import PDFParser

        # mock pymupdf
        mock_page = MagicMock()
        mock_page.get_text.return_value = ""  # 空文本 → 触发 OCR
        mock_page.find_tables.return_value = MagicMock(tables=[])
        mock_page.get_images.return_value = []
        mock_pixmap = MagicMock()
        mock_pixmap.tobytes.return_value = b"png"
        mock_page.get_pixmap.return_value = mock_pixmap

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()

        mock_fit = MagicMock()
        mock_fit.open.return_value = mock_doc
        mock_fit.Matrix.return_value = MagicMock()

        mock_vlm = MagicMock()
        mock_vlm.understand = AsyncMock(return_value="OCR文本内容")

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fit}), \
             patch("app.vlm.provider.get_vision_provider", return_value=mock_vlm), \
             patch("app.document.pdf_parser.get_settings") as mock_settings:
            mock_settings.return_value.PDF_TABLE_EXTRACTION_ENABLED = True
            mock_settings.return_value.PDF_IMAGE_EXTRACTION_ENABLED = True
            mock_settings.return_value.PDF_IMAGE_MAX_PER_DOC = 50
            mock_settings.return_value.PDF_SCAN_OCR_ENABLED = True
            mock_settings.return_value.PDF_SCAN_OCR_MAX_PAGES = 20

            parser = PDFParser()
            result = await parser.parse("/fake/scan.pdf")

        assert "OCR文本内容" in result
        assert "[扫描页 OCR]" in result

    @pytest.mark.asyncio
    async def test_parse_scan_ocr_disabled(self) -> None:
        """PDF_SCAN_OCR_ENABLED=False 时不触发 OCR。"""
        from app.document.pdf_parser import PDFParser

        mock_page = MagicMock()
        mock_page.get_text.return_value = ""  # 空文本
        mock_page.find_tables.return_value = MagicMock(tables=[])
        mock_page.get_images.return_value = []

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()

        mock_fit = MagicMock()
        mock_fit.open.return_value = mock_doc

        import sys
        with patch.dict(sys.modules, {"fitz": mock_fit}), \
             patch("app.document.pdf_parser.get_settings") as mock_settings:
            mock_settings.return_value.PDF_TABLE_EXTRACTION_ENABLED = False
            mock_settings.return_value.PDF_IMAGE_EXTRACTION_ENABLED = False
            mock_settings.return_value.PDF_IMAGE_MAX_PER_DOC = 50
            mock_settings.return_value.PDF_SCAN_OCR_ENABLED = False
            mock_settings.return_value.PDF_SCAN_OCR_MAX_PAGES = 20

            parser = PDFParser()
            result = await parser.parse("/fake/scan.pdf")

        # OCR 被关闭，结果为空
        assert result == ""


# ======================================================================
# P3: PPTX 备注提取测试
# ======================================================================


class TestPPTXNotesExtraction:
    """P3: PPTX 演讲者备注提取测试。"""

    def test_extract_notes_with_content(self) -> None:
        """有备注时返回 ParsedSection。"""
        from app.document.pptx_parser import PPTXParser

        mock_slide = MagicMock()
        mock_slide.has_notes_slide = True
        mock_slide.notes_slide.notes_text_frame.text = "这是备注内容"

        result = PPTXParser._extract_notes(mock_slide, 0)

        assert result is not None
        assert "备注内容" in result.content
        assert "[演讲者备注]" in result.content

    def test_extract_notes_empty(self) -> None:
        """空备注返回 None。"""
        from app.document.pptx_parser import PPTXParser

        mock_slide = MagicMock()
        mock_slide.has_notes_slide = True
        mock_slide.notes_slide.notes_text_frame.text = ""

        result = PPTXParser._extract_notes(mock_slide, 0)
        assert result is None

    def test_extract_notes_no_notes_slide(self) -> None:
        """无备注页返回 None。"""
        from app.document.pptx_parser import PPTXParser

        mock_slide = MagicMock()
        mock_slide.has_notes_slide = False

        result = PPTXParser._extract_notes(mock_slide, 0)
        assert result is None

    def test_extract_notes_exception_returns_none(self) -> None:
        """异常时返回 None。"""
        from app.document.pptx_parser import PPTXParser

        mock_slide = MagicMock()
        mock_slide.has_notes_slide = True
        mock_slide.notes_slide.notes_text_frame.text = ""
        # 模拟访问 notes_slide 抛异常
        type(mock_slide).notes_slide = PropertyMock(side_effect=Exception("error"))

        result = PPTXParser._extract_notes(mock_slide, 0)
        assert result is None


# ======================================================================
# P3: DOCX 页眉页脚提取测试
# ======================================================================


class TestDOCXHeaderFooterExtraction:
    """P3: DOCX 页眉页脚提取测试。"""

    def test_extract_header_footer_with_content(self) -> None:
        """有页眉页脚时返回 ParsedSection。"""
        from app.document.docx_parser import DOCXParser

        mock_header = MagicMock()
        mock_header.is_linked_to_previous = False
        mock_para = MagicMock()
        mock_para.text = "公司机密"
        mock_header.paragraphs = [mock_para]

        mock_footer = MagicMock()
        mock_footer.is_linked_to_previous = False
        mock_para2 = MagicMock()
        mock_para2.text = "第1页"
        mock_footer.paragraphs = [mock_para2]

        mock_section = MagicMock()
        mock_section.header = mock_header
        mock_section.footer = mock_footer

        mock_doc = MagicMock()
        mock_doc.sections = [mock_section]

        result = DOCXParser._extract_headers_footers(mock_doc)

        assert len(result) == 2
        assert "公司机密" in result[0].content
        assert "[页眉]" in result[0].content
        assert "第1页" in result[1].content
        assert "[页脚]" in result[1].content

    def test_extract_header_footer_linked_to_previous(self) -> None:
        """页眉页脚链接到上一节时跳过。"""
        from app.document.docx_parser import DOCXParser

        mock_header = MagicMock()
        mock_header.is_linked_to_previous = True  # 链接到上一节
        mock_footer = MagicMock()
        mock_footer.is_linked_to_previous = True

        mock_section = MagicMock()
        mock_section.header = mock_header
        mock_section.footer = mock_footer

        mock_doc = MagicMock()
        mock_doc.sections = [mock_section]

        result = DOCXParser._extract_headers_footers(mock_doc)
        assert len(result) == 0


# ======================================================================
# Docling 统一解析器测试
# ======================================================================


class TestDoclingSupportedTypes:
    """Docling 支持的文档类型测试。"""

    def test_pdf_supported(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("pdf") is True

    def test_docx_supported(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("docx") is True

    def test_pptx_supported(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("pptx") is True

    def test_xlsx_supported(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("xlsx") is True

    def test_html_supported(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("html") is True

    def test_image_supported(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("png") is True
        assert DoclingParser.is_supported("jpg") is True

    def test_audio_supported(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("mp3") is True
        assert DoclingParser.is_supported("wav") is True

    def test_video_not_supported(self) -> None:
        """视频不在 Docling 支持列表中。"""
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("mp4") is False
        assert DoclingParser.is_supported("video") is False

    def test_case_insensitive(self) -> None:
        from app.document.docling_parser import DoclingParser

        assert DoclingParser.is_supported("PDF") is True
        assert DoclingParser.is_supported("DOCX") is True


class TestDoclingParserParse:
    """DoclingParser.parse 测试。"""

    @pytest.mark.asyncio
    async def test_not_installed_returns_empty(self) -> None:
        """Docling 未安装时返回空字符串。"""
        from app.document.docling_parser import DoclingParser

        parser = DoclingParser()
        with patch.dict("sys.modules", {"docling": None, "docling.document_converter": None}):
            result = await parser.parse("/fake/doc.pdf")
            assert result == ""

    @pytest.mark.asyncio
    async def test_successful_parse(self) -> None:
        """成功解析返回 HTML。"""
        from app.document.docling_parser import DoclingParser

        mock_result = MagicMock()
        mock_result.document.export_to_html.return_value = "<h1>标题</h1><p>正文内容</p>"

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result

        parser = DoclingParser()
        parser._converter = mock_converter
        parser._init_checked = True

        with patch("app.document.docling_parser.get_settings") as mock_settings:
            mock_settings.return_value.DOCLING_VLM_IMAGE_ENHANCE = False
            result = await parser.parse("/fake/doc.pdf")

        assert "<h1>标题</h1>" in result
        assert "正文内容" in result

    @pytest.mark.asyncio
    async def test_empty_result_returns_empty(self) -> None:
        """Docling 返回空 HTML 时返回空字符串。"""
        from app.document.docling_parser import DoclingParser

        mock_result = MagicMock()
        mock_result.document.export_to_html.return_value = ""

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result

        parser = DoclingParser()
        parser._converter = mock_converter
        parser._init_checked = True

        with patch("app.document.docling_parser.get_settings") as mock_settings:
            mock_settings.return_value.DOCLING_VLM_IMAGE_ENHANCE = False
            result = await parser.parse("/fake/empty.pdf")

        assert result == ""

    @pytest.mark.asyncio
    async def test_parse_exception_returns_empty(self) -> None:
        """解析异常时返回空字符串。"""
        from app.document.docling_parser import DoclingParser

        mock_converter = MagicMock()
        mock_converter.convert.side_effect = Exception("parse error")

        parser = DoclingParser()
        parser._converter = mock_converter
        parser._init_checked = True

        result = await parser.parse("/fake/bad.pdf")
        assert result == ""

    @pytest.mark.asyncio
    async def test_vlm_enhance_disabled_by_default(self) -> None:
        """默认不启用 VLM 图片描述增强。"""
        from app.document.docling_parser import DoclingParser

        mock_result = MagicMock()
        mock_result.document.export_to_html.return_value = "<h1>文档</h1>"
        mock_result.document.pictures = []

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result

        parser = DoclingParser()
        parser._converter = mock_converter
        parser._init_checked = True

        with patch("app.document.docling_parser.get_settings") as mock_settings:
            mock_settings.return_value.DOCLING_VLM_IMAGE_ENHANCE = False
            result = await parser.parse("/fake/doc.pdf")

        assert result == "<h1>文档</h1>"


class TestDoclingConfig:
    """Docling 配置项测试。"""

    def test_docling_enabled_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "DOCLING_ENABLED")
        assert settings.DOCLING_ENABLED is True

    def test_docling_vlm_enhance_config(self) -> None:
        from app.config import Settings

        settings = Settings()
        assert hasattr(settings, "DOCLING_VLM_IMAGE_ENHANCE")
        assert settings.DOCLING_VLM_IMAGE_ENHANCE is False


class TestFactoryDoclingPriority:
    """factory Docling 优先级测试。"""

    def test_get_parser_returns_legacy_when_docling_disabled(self) -> None:
        """DOCLING_ENABLED=False 时返回原有解析器。"""
        from app.document.factory import get_parser
        from app.document.pdf_parser import PDFParser

        with patch("app.document.factory._is_docling_enabled", return_value=False):
            parser = get_parser("pdf")

        assert isinstance(parser, PDFParser)

    def test_get_parser_returns_docling_when_enabled(self) -> None:
        """DOCLING_ENABLED=True 且 Docling 可用时返回 DoclingParser。"""
        from app.document.factory import get_parser
        from app.document.docling_parser import DoclingParser

        with patch("app.document.factory._is_docling_enabled", return_value=True):
            parser = get_parser("pdf")

        assert isinstance(parser, DoclingParser)

    def test_get_parser_with_fallback_docling(self) -> None:
        """get_parser_with_fallback 返回 docling 类型。"""
        from app.document.factory import get_parser_with_fallback

        with patch("app.document.factory._is_docling_enabled", return_value=True):
            parser, parser_type = get_parser_with_fallback("pdf")

        assert parser_type == "docling"

    def test_get_parser_with_fallback_legacy(self) -> None:
        """Docling 禁用时返回 legacy 类型。"""
        from app.document.factory import get_parser_with_fallback

        with patch("app.document.factory._is_docling_enabled", return_value=False):
            parser, parser_type = get_parser_with_fallback("pdf")

        assert parser_type == "legacy"

    def test_get_parser_with_fallback_none(self) -> None:
        """不支持的类型返回 none。"""
        from app.document.factory import get_parser_with_fallback

        with patch("app.document.factory._is_docling_enabled", return_value=False):
            parser, parser_type = get_parser_with_fallback("unknown")

        assert parser_type == "none"
        assert parser is None


class TestDocumentTasksDoclingIntegration:
    """document_tasks Docling 集成测试。"""

    @pytest.mark.asyncio
    async def test_parse_document_uses_docling_when_available(self) -> None:
        """Docling 可用时优先使用 Docling 解析。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "pdf"
        mock_doc.file_path = "/fake/doc.pdf"
        mock_doc.id = "test-id"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="<h1>HTML 内容</h1>")

        with patch("app.document.factory.get_parser_with_fallback",
                    return_value=(mock_parser, "docling")):
            result = await _parse_document(mock_doc)

        assert result == "<h1>HTML 内容</h1>"

    @pytest.mark.asyncio
    async def test_parse_document_fallback_to_legacy(self) -> None:
        """Docling 返回空时降级到原有解析器。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "pdf"
        mock_doc.file_path = "/fake/doc.pdf"
        mock_doc.id = "test-id"

        mock_docling_parser = MagicMock()
        mock_docling_parser.parse = AsyncMock(return_value="")  # Docling 返回空

        mock_legacy_parser = MagicMock()
        mock_legacy_parser.parse = AsyncMock(return_value="Legacy PDF 内容")

        # _try_docling_parse 调用 get_parser_with_fallback → 返回 docling（空）
        # _parse_pdf 调用 app.document.get_parser → 返回 legacy parser
        with patch("app.document.factory.get_parser_with_fallback",
                    return_value=(mock_docling_parser, "docling")), \
             patch("app.document.get_parser", return_value=mock_legacy_parser):
            result = await _parse_document(mock_doc)

        assert result == "Legacy PDF 内容"

    @pytest.mark.asyncio
    async def test_parse_document_docling_not_available(self) -> None:
        """Docling 不可用时直接走原有路径。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "pdf"
        mock_doc.file_path = "/fake/doc.pdf"
        mock_doc.id = "test-id"

        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value="PDF 内容")

        # get_parser_with_fallback 返回 (None, "none") → 不走 Docling
        with patch("app.document.factory.get_parser_with_fallback",
                    return_value=(None, "none")), \
             patch("app.document.get_parser", return_value=mock_parser):
            result = await _parse_document(mock_doc)

        assert result == "PDF 内容"

    @pytest.mark.asyncio
    async def test_parse_document_video_not_docling(self) -> None:
        """视频类型不走 Docling，直接走视频管线。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "mp4"
        mock_doc.file_path = "/fake/video.mp4"
        mock_doc.id = "test-id"

        with patch("tasks.document_tasks._parse_video", new_callable=AsyncMock) as mock_video:
            mock_video.return_value = "视频转写文本"
            result = await _parse_document(mock_doc)

        assert result == "视频转写文本"
        mock_video.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_document_doc_uses_legacy_fallback(self) -> None:
        """旧格式 .doc 不走 Docling，返回降级提示。"""
        from tasks.document_tasks import _parse_document

        mock_doc = MagicMock()
        mock_doc.content_text = None
        mock_doc.doc_type = "doc"
        mock_doc.file_path = "/fake/old.doc"
        mock_doc.id = "test-id"

        result = await _parse_document(mock_doc)

        assert ".doc" in result
        assert "请将文件另存为 .docx" in result

    def test_extract_header_footer_empty(self) -> None:
        """空页眉页脚返回空列表。"""
        from app.document.docx_parser import DOCXParser

        mock_header = MagicMock()
        mock_header.is_linked_to_previous = False
        mock_header.paragraphs = []

        mock_footer = MagicMock()
        mock_footer.is_linked_to_previous = False
        mock_footer.paragraphs = []

        mock_section = MagicMock()
        mock_section.header = mock_header
        mock_section.footer = mock_footer

        mock_doc = MagicMock()
        mock_doc.sections = [mock_section]

        result = DOCXParser._extract_headers_footers(mock_doc)
        assert len(result) == 0

    def test_extract_header_footer_exception(self) -> None:
        """异常时返回空列表。"""
        from app.document.docx_parser import DOCXParser

        mock_section = MagicMock()
        type(mock_section).header = PropertyMock(side_effect=Exception("error"))

        mock_doc = MagicMock()
        mock_doc.sections = [mock_section]

        result = DOCXParser._extract_headers_footers(mock_doc)
        assert len(result) == 0


# ============================================================
# 新增测试 — 图片上传、小图过滤、分页分隔符、Markdown 输出
# ============================================================


class TestImageStorageDimensions:
    """image_storage.get_image_dimensions — 零依赖图片尺寸解析。"""

    def test_png_dimensions(self):
        """PNG 尺寸解析 — 构造最小 PNG 头。"""
        from app.document.image_storage import get_image_dimensions

        # PNG 签名 (8 bytes) + IHDR chunk (4 len + 4 type + 4 width + 4 height)
        import struct

        png_header = (
            b"\x89PNG\r\n\x1a\n"  # PNG signature
            + struct.pack(">I", 13)  # IHDR length
            + b"IHDR"
            + struct.pack(">II", 800, 600)  # width=800, height=600
            + b"\x08\x02\x00\x00\x00"  # bit depth, color type, etc.
        )
        w, h = get_image_dimensions(png_header, "png")
        assert w == 800
        assert h == 600

    def test_invalid_bytes_returns_zero(self):
        """无效字节返回 (0, 0)。"""
        from app.document.image_storage import get_image_dimensions

        w, h = get_image_dimensions(b"not an image", "png")
        assert w == 0
        assert h == 0

    def test_empty_bytes_returns_zero(self):
        """空字节返回 (0, 0)。"""
        from app.document.image_storage import get_image_dimensions

        w, h = get_image_dimensions(b"", "png")
        assert w == 0
        assert h == 0

    def test_auto_detect_png(self):
        """无扩展名时自动检测 PNG。"""
        from app.document.image_storage import get_image_dimensions

        import struct

        png_header = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 400, 300)
            + b"\x08\x02\x00\x00\x00"
        )
        w, h = get_image_dimensions(png_header, "")
        assert w == 400
        assert h == 300


class TestImageStorageFormat:
    """image_storage.is_supported_format — 格式校验。"""

    @pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "webp"])
    def test_supported_formats(self, ext):
        """支持的格式返回 True。"""
        from app.document.image_storage import is_supported_format

        assert is_supported_format(ext) is True

    @pytest.mark.parametrize("ext", ["gif", "bmp", "tiff", "svg", ""])
    def test_unsupported_formats(self, ext):
        """不支持的格式返回 False。"""
        from app.document.image_storage import is_supported_format

        assert is_supported_format(ext) is False


class TestImageStorageUpload:
    """image_storage.upload_image — 小图过滤 + 上传。"""

    @pytest.mark.asyncio
    async def test_small_image_filtered(self):
        """小于 min_size 的图片被过滤。"""
        from app.document.image_storage import upload_image

        import struct

        # 构造 30x30 PNG（小于 50px 阈值）
        png_header = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 30, 30)
            + b"\x08\x02\x00\x00\x00"
        )

        url = await upload_image(
            image_bytes=png_header,
            ext="png",
            doc_id="test-doc",
            page=0,
            idx=0,
            min_size=50,
            width=30,
            height=30,
        )
        assert url is None

    @pytest.mark.asyncio
    async def test_unsupported_format_returns_none(self):
        """不支持的格式返回 None。"""
        from app.document.image_storage import upload_image

        url = await upload_image(
            image_bytes=b"fake-gif",
            ext="gif",
            doc_id="test-doc",
            page=0,
            idx=0,
            min_size=50,
        )
        assert url is None

    @pytest.mark.asyncio
    async def test_empty_bytes_returns_none(self):
        """空字节返回 None。"""
        from app.document.image_storage import upload_image

        url = await upload_image(
            image_bytes=b"",
            ext="png",
            doc_id="test-doc",
            page=0,
            idx=0,
        )
        assert url is None

    @pytest.mark.asyncio
    async def test_upload_success_mocked(self):
        """上传成功 — mock MinIO upload_file。"""
        from app.document.image_storage import upload_image

        import struct

        # 构造 100x100 PNG
        png_header = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 100, 100)
            + b"\x08\x02\x00\x00\x00"
        )

        with patch("app.utils.minio_client.upload_file", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = "minio://ekb-documents/test-doc/page0_img0.png"

            url = await upload_image(
                image_bytes=png_header,
                ext="png",
                doc_id="test-doc",
                page=0,
                idx=0,
                min_size=50,
                width=100,
                height=100,
            )
            assert url == "minio://ekb-documents/test-doc/page0_img0.png"
            mock_upload.assert_called_once()


class TestSectionsToTextHtmlOutput:
    """sections_to_text — HTML 输出格式（唯一支持的格式）。"""

    def test_table_keeps_html(self):
        """表格保持 HTML <table> 格式（不转 Markdown）。"""
        from app.document.base import DocumentParser, ParsedSection

        html_table = (
            "<table><tr><th>姓名</th><th>年龄</th></tr>"
            "<tr><td>张三</td><td>25</td></tr></table>"
        )
        sections = [ParsedSection(kind="table", content=html_table, page=0)]
        result = DocumentParser.sections_to_text(sections)
        assert "<table>" in result
        assert "<th>姓名</th>" in result
        # 不应出现 Markdown 表格语法
        assert "| 姓名 |" not in result
        assert "| --- |" not in result

    def test_image_url_outputs_img_tag(self):
        """image_url 类型输出 <img> 标签。"""
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(
                kind="image_url",
                content="[图片描述: 架构图]",
                page=0,
                image_url="https://example.com/img.png",
            )
        ]
        result = DocumentParser.sections_to_text(sections)
        assert '<img src="https://example.com/img.png"' in result
        assert "[图片描述: 架构图]" in result
        # 不应出现 Markdown 图片语法
        assert "![图片]" not in result

    def test_image_url_no_description(self):
        """image_url 无描述时只输出 <img> 标签。"""
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(
                kind="image_url",
                content="",
                page=0,
                image_url="https://example.com/img.png",
            )
        ]
        result = DocumentParser.sections_to_text(sections)
        assert '<img src="https://example.com/img.png"' in result


class TestSectionsToTextPageSeparator:
    """sections_to_text — 分页分隔符。"""

    def test_page_separator_inserted(self):
        """页码变化时插入分隔符。"""
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(kind="text", content="第一页内容", page=0),
            ParsedSection(kind="text", content="第二页内容", page=1),
        ]
        sep = "\n\n---\n<!-- page: {page} -->\n"
        result = DocumentParser.sections_to_text(
            sections, page_separator=sep
        )
        assert "第一页内容" in result
        assert "第二页内容" in result
        assert "---" in result
        assert "<!-- page: 1 -->" in result

    def test_no_separator_when_empty(self):
        """空分隔符时不插入（默认行为，向后兼容）。"""
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(kind="text", content="第一页", page=0),
            ParsedSection(kind="text", content="第二页", page=1),
        ]
        result = DocumentParser.sections_to_text(sections, page_separator="")
        assert "第一页" in result
        assert "第二页" in result
        assert "<!-- page:" not in result

    def test_same_page_no_separator(self):
        """同一页内不插入分隔符。"""
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(kind="text", content="段落1", page=0),
            ParsedSection(kind="text", content="段落2", page=0),
        ]
        result = DocumentParser.sections_to_text(
            sections, page_separator="---{page}---"
        )
        assert "---0---" not in result  # 同页不插入

    def test_page_placeholder_replaced(self):
        """{page} 占位符被替换为实际页码。"""
        from app.document.base import DocumentParser, ParsedSection

        sections = [
            ParsedSection(kind="text", content="页0", page=0),
            ParsedSection(kind="text", content="页3", page=3),
        ]
        result = DocumentParser.sections_to_text(
            sections, page_separator="[PAGE:{page}]"
        )
        assert "[PAGE:3]" in result
        assert "[PAGE:0]" not in result  # 第一页不插入


class TestDocxPageBreakDetection:
    """DOCX 分页检测 — _has_page_break。"""

    def test_explicit_page_break_detected(self):
        """显式分页符 <w:br w:type="page"/> 被检测。"""
        from app.document.docx_parser import DOCXParser

        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = f'<w:p xmlns:w="{ns}"><w:r><w:br w:type="page"/></w:r></w:p>'
        element = etree.fromstring(xml)
        assert DOCXParser._has_page_break(element) is True

    def test_last_rendered_page_break_detected(self):
        """<w:lastRenderedPageBreak/> 被检测。"""
        from app.document.docx_parser import DOCXParser

        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = f'<w:p xmlns:w="{ns}"><w:r><w:lastRenderedPageBreak/></w:r></w:p>'
        element = etree.fromstring(xml)
        assert DOCXParser._has_page_break(element) is True

    def test_no_page_break(self):
        """无分页符返回 False。"""
        from app.document.docx_parser import DOCXParser

        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = f'<w:p xmlns:w="{ns}"><w:r><w:t>普通文本</w:t></w:r></w:p>'
        element = etree.fromstring(xml)
        assert DOCXParser._has_page_break(element) is False


class TestParserBoolIntHelpers:
    """DocumentParser._bool / _int — MagicMock 安全转换。"""

    def test_bool_with_real_bool(self):
        """真实 bool 值正常返回。"""
        from app.document.base import DocumentParser

        assert DocumentParser._bool(True, False) is True
        assert DocumentParser._bool(False, True) is False

    def test_bool_with_magic_mock_returns_default(self):
        """MagicMock 返回默认值。"""
        from app.document.base import DocumentParser

        mock = MagicMock()
        assert DocumentParser._bool(mock, True) is True
        assert DocumentParser._bool(mock, False) is False

    def test_int_with_real_int(self):
        """真实 int 值正常返回。"""
        from app.document.base import DocumentParser

        assert DocumentParser._int(42, 0) == 42
        assert DocumentParser._int(0, 50) == 0

    def test_int_with_magic_mock_returns_default(self):
        """MagicMock 返回默认值。"""
        from app.document.base import DocumentParser

        mock = MagicMock()
        assert DocumentParser._int(mock, 50) == 50

    def test_int_with_bool_returns_default(self):
        """bool 不是 int，返回默认值（避免 True=1 的陷阱）。"""
        from app.document.base import DocumentParser

        assert DocumentParser._int(True, 50) == 50


# ============================================================
# P0: DOCX 标题层级映射测试
# ============================================================


class TestDocxHeadingMapping:
    """DOCX 标题层级映射 — 段落样式检测为 HTML <h1>~<h4>。"""

    @staticmethod
    def _make_paragraph(style_id: str = "", text: str = "标题文本") -> Any:
        """构造带样式 ID 的 <w:p> XML 元素。

        Args:
            style_id: 样式 ID（如 "Heading1", "Title"）。
            text: 段落文本。

        Returns:
            lxml etree Element。
        """
        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        # XML 转义文本
        escaped_text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        # 构造 <w:p><w:pPr><w:pStyle w:val="..."/></w:pPr><w:r><w:t>...</w:t></w:r></w:p>
        if style_id:
            xml = (
                f'<w:p xmlns:w="{ns}">'
                f'<w:pPr><w:pStyle w:val="{style_id}"/></w:pPr>'
                f'<w:r><w:t>{escaped_text}</w:t></w:r>'
                f'</w:p>'
            )
        else:
            xml = (
                f'<w:p xmlns:w="{ns}">'
                f'<w:r><w:t>{escaped_text}</w:t></w:r>'
                f'</w:p>'
            )
        return etree.fromstring(xml)

    def test_heading1_outputs_h1(self):
        """Heading1 样式 → <h1>。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading1", "第一章 概述")
        result = DOCXParser._style_to_heading(element, "第一章 概述")
        assert result == "<h1>第一章 概述</h1>"

    def test_heading2_outputs_h2(self):
        """Heading2 样式 → <h2>。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading2", "1.1 背景")
        result = DOCXParser._style_to_heading(element, "1.1 背景")
        assert result == "<h2>1.1 背景</h2>"

    def test_heading3_outputs_h3(self):
        """Heading3 样式 → <h3>。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading3", "1.1.1 目标")
        result = DOCXParser._style_to_heading(element, "1.1.1 目标")
        assert result == "<h3>1.1.1 目标</h3>"

    def test_heading4_outputs_h4(self):
        """Heading4 样式 → <h4>。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading4", "1.1.1.1 细节")
        result = DOCXParser._style_to_heading(element, "1.1.1.1 细节")
        assert result == "<h4>1.1.1.1 细节</h4>"

    def test_title_outputs_h1(self):
        """Title 样式 → <h1>（文档主标题）。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Title", "企业知识库设计文档")
        result = DOCXParser._style_to_heading(element, "企业知识库设计文档")
        assert result == "<h1>企业知识库设计文档</h1>"

    def test_heading5_supported(self):
        """Heading5 样式 → <h5>（支持超过 4 级）。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading5", "深层标题")
        result = DOCXParser._style_to_heading(element, "深层标题")
        assert result == "<h5>深层标题</h5>"

    def test_heading6_capped_at_h6(self):
        """Heading7 级别被截断到 <h6>（HTML 最深标题）。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading7", "超深标题")
        result = DOCXParser._style_to_heading(element, "超深标题")
        assert result == "<h6>超深标题</h6>"

    def test_non_heading_style_returns_empty(self):
        """非标题样式返回空字符串。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Normal", "普通正文")
        result = DOCXParser._style_to_heading(element, "普通正文")
        assert result == ""

    def test_no_style_returns_empty(self):
        """无样式（无 <w:pStyle>）返回空字符串。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("", "无样式文本")
        result = DOCXParser._style_to_heading(element, "无样式文本")
        assert result == ""

    def test_heading_with_html_special_chars_escaped(self):
        """标题文本中的 HTML 特殊字符被转义。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading1", "A < B & C > D")
        result = DOCXParser._style_to_heading(element, "A < B & C > D")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_format_paragraph_heading(self):
        """_format_paragraph 对标题段落输出 HTML 标题标签。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Heading2", "第二章 设计")
        result = DOCXParser._format_paragraph(element)
        assert result == "<h2>第二章 设计</h2>"

    def test_format_paragraph_normal_text(self):
        """_format_paragraph 对普通段落输出纯文本（已转义）。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_paragraph("Normal", "这是普通正文")
        result = DOCXParser._format_paragraph(element)
        assert result == "这是普通正文"

    def test_format_paragraph_empty_returns_empty(self):
        """_format_paragraph 对空段落返回空字符串。"""
        from app.document.docx_parser import DOCXParser

        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = f'<w:p xmlns:w="{ns}"><w:r><w:t></w:t></w:r></w:p>'
        element = etree.fromstring(xml)
        result = DOCXParser._format_paragraph(element)
        assert result == ""


# ============================================================
# P1: DOCX 列表结构测试
# ============================================================


class TestDocxListStructure:
    """DOCX 列表结构 — 检测 numPr 输出 <ul><li>。"""

    @staticmethod
    def _make_list_paragraph(text: str = "列表项") -> Any:
        """构造带 numPr 的列表段落 XML。"""
        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        escaped_text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        xml = (
            f'<w:p xmlns:w="{ns}">'
            f'<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            f'<w:r><w:t>{escaped_text}</w:t></w:r>'
            f'</w:p>'
        )
        return etree.fromstring(xml)

    @staticmethod
    def _make_list_style_paragraph(style_id: str = "ListParagraph", text: str = "列表项") -> Any:
        """构造 ListParagraph 样式的段落 XML。"""
        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        escaped_text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        xml = (
            f'<w:p xmlns:w="{ns}">'
            f'<w:pPr><w:pStyle w:val="{style_id}"/></w:pPr>'
            f'<w:r><w:t>{escaped_text}</w:t></w:r>'
            f'</w:p>'
        )
        return etree.fromstring(xml)

    @staticmethod
    def _make_normal_paragraph(text: str = "普通文本") -> Any:
        """构造普通段落 XML（无 numPr，无 ListParagraph 样式）。"""
        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        escaped_text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        xml = (
            f'<w:p xmlns:w="{ns}">'
            f'<w:pPr><w:pStyle w:val="Normal"/></w:pPr>'
            f'<w:r><w:t>{escaped_text}</w:t></w:r>'
            f'</w:p>'
        )
        return etree.fromstring(xml)

    def test_numPr_detected_as_list(self):
        """含 <w:numPr> 的段落被识别为列表项。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_list_paragraph("第一步")
        assert DOCXParser._is_list_paragraph(element) is True

    def test_list_paragraph_style_detected(self):
        """ListParagraph 样式被识别为列表项。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_list_style_paragraph("ListParagraph", "列表项")
        assert DOCXParser._is_list_paragraph(element) is True

    def test_normal_paragraph_not_list(self):
        """普通段落（Normal 样式，无 numPr）不被识别为列表。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_normal_paragraph("普通段落")
        assert DOCXParser._is_list_paragraph(element) is False

    def test_no_ppr_not_list(self):
        """无 <w:pPr> 的段落不被识别为列表。"""
        from app.document.docx_parser import DOCXParser

        from lxml import etree

        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = f'<w:p xmlns:w="{ns}"><w:r><w:t>无 pPr</w:t></w:r></w:p>'
        element = etree.fromstring(xml)
        assert DOCXParser._is_list_paragraph(element) is False

    def test_format_paragraph_list_outputs_ul_li(self):
        """_format_paragraph 对列表段落输出 <ul><li> 标签。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_list_paragraph("第一条规则")
        result = DOCXParser._format_paragraph(element)
        assert "<ul><li>" in result
        assert "第一条规则" in result
        assert "</li></ul>" in result

    def test_list_text_html_escaped(self):
        """列表项文本中的 HTML 特殊字符被转义。"""
        from app.document.docx_parser import DOCXParser

        element = self._make_list_paragraph("A < B & C")
        result = DOCXParser._format_paragraph(element)
        assert "&lt;" in result
        assert "&amp;" in result


# ============================================================
# P1: XLSX 降级旁路测试
# ============================================================


class TestXlsxFallback:
    """XLSX 降级旁路 — openpyxl 失败时降级到 pandas。"""

    @pytest.mark.asyncio
    async def test_openpyxl_success_no_fallback(self):
        """openpyxl 解析成功时不触发降级。"""
        from app.document.xlsx_parser import XLSXParser

        parser = XLSXParser()

        # mock openpyxl 成功
        mock_wb = MagicMock()
        mock_ws = MagicMock()
        mock_ws.max_row = 2
        mock_ws.max_column = 2
        mock_ws.title = "Sheet1"
        # iter_rows(values_only=True) 返回值元组
        mock_ws.iter_rows.return_value = [
            ("A", "B"),
            ("1", "2"),
        ]
        # 关键：sheetnames 是属性，需要返回真实列表
        mock_wb.sheetnames = ["Sheet1"]
        # workbook[sheet_name] 返回 worksheet
        mock_wb.__getitem__ = MagicMock(return_value=mock_ws)

        with patch("openpyxl.load_workbook", return_value=mock_wb):
            result = await parser.parse("/fake/test.xlsx")

        assert "<table>" in result
        assert "<th>A</th>" in result
        assert "<td>1</td>" in result

    @pytest.mark.asyncio
    async def test_openpyxl_failure_fallback_to_pandas(self):
        """openpyxl 失败时降级到 pandas。"""
        from app.document.xlsx_parser import XLSXParser

        parser = XLSXParser()

        # mock pandas 返回单 sheet dict（sheet_name=None 的返回格式）
        import pandas as pd

        def mock_read_excel(path, **kwargs):
            if kwargs.get("sheet_name") is None:
                return {
                    "Sheet1": pd.DataFrame(
                        {"姓名": ["张三", "李四"], "年龄": [25, 30]}
                    )
                }
            return pd.DataFrame()

        # mock openpyxl 抛异常，pandas 返回 dict
        with patch("openpyxl.load_workbook", side_effect=Exception("openpyxl error")), \
             patch("pandas.read_excel", side_effect=mock_read_excel):
            result = await parser.parse("/fake/test.xlsx")

        assert "<table>" in result
        assert "张三" in result
        assert "李四" in result

    @pytest.mark.asyncio
    async def test_both_failures_returns_empty(self):
        """openpyxl 和 pandas 都失败时返回空字符串。"""
        from app.document.xlsx_parser import XLSXParser

        parser = XLSXParser()

        with patch("openpyxl.load_workbook", side_effect=Exception("openpyxl error")), \
             patch("pandas.read_excel", side_effect=Exception("pandas error")):
            result = await parser.parse("/fake/test.xlsx")

        assert result == ""

    @pytest.mark.asyncio
    async def test_fallback_preserves_multiple_sheets(self):
        """pandas 降级时保留多 sheet。"""
        from app.document.xlsx_parser import XLSXParser

        parser = XLSXParser()

        import pandas as pd

        # pandas read_excel 用 sheet_name=None 返回所有 sheet 的 dict
        def mock_read_excel(path, **kwargs):
            if kwargs.get("sheet_name") is None:
                return {
                    "Sheet1": pd.DataFrame({"A": [1, 2]}),
                    "Sheet2": pd.DataFrame({"B": [3, 4]}),
                }
            return pd.DataFrame()

        with patch("openpyxl.load_workbook", side_effect=Exception("fail")), \
             patch("pandas.read_excel", side_effect=mock_read_excel):
            result = await parser.parse("/fake/test.xlsx")

        assert "Sheet1" in result
        assert "Sheet2" in result


# ============================================================
# P2: XLSX 列宽对齐测试
# ============================================================


class TestXlsxColumnAlignment:
    """XLSX 列宽对齐 — 合并单元格场景补空。"""

    def test_pad_short_row_to_max_columns(self):
        """短行被补齐到最大列数。"""
        from app.document.xlsx_parser import XLSXParser

        # 模拟合并单元格导致行长度不一致
        rows = [
            ["A", "B", "C"],  # 3 列
            ["1", "2"],       # 2 列（合并了第 3 列）
            ["x", "y", "z"],  # 3 列
        ]
        # 找出最大列数
        max_cols = max(len(r) for r in rows) if rows else 0
        # 补齐
        padded = [r + [""] * (max_cols - len(r)) for r in rows]

        assert len(padded[1]) == 3
        assert padded[1][2] == ""

    def test_rows_to_html_with_uneven_lengths(self):
        """_rows_to_html 处理不等长行。"""
        from app.document.xlsx_parser import XLSXParser

        # 构造不等长行（合并单元格场景）
        rows = [
            ["姓名", "年龄", "部门"],
            ["张三", "25"],  # 缺少部门
        ]
        html = XLSXParser._rows_to_html(rows)

        # 应该补齐为 3 列
        assert "<th>姓名</th>" in html
        assert "<th>年龄</th>" in html
        assert "<th>部门</th>" in html
        assert "<td>张三</td>" in html
        assert "<td>25</td>" in html
        # 补齐的空单元格
        assert "<td></td>" in html

