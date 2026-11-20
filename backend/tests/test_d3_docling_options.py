"""D3 闸门 — Docling 显式 PdfPipelineOptions（表格模式/OCR 开关真实传入）。

红基线契约：DocumentConverter() 无参构造，默认 TableFormer ACCURATE + do_ocr=True，
配置零利用。修复后契约：format_options[InputFormat.PDF] 携带真实
PdfPipelineOptions，DOCLING_TABLE_MODE 默认 FAST、DOCLING_DO_OCR 可关。

边界说明：本机 torch 动态库缺失（maclean 去重事故，见 task.md 勘误），
docling.document_converter 真实导入链断；converter 边界用捕获替身，
但断言侧用真实 docling.datamodel pydantic 模型/枚举（可独立导入，非同源镜像）。
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（B10 教训：httpx trust_env 解析即炸）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


@pytest.fixture
def captured_converter(monkeypatch):
    """捕获 DocumentConverter 构造参数的替身模块。"""
    captured: dict = {}

    class _StubFormatOption:
        def __init__(self, pipeline_options=None, **kwargs):
            self.pipeline_options = pipeline_options

    class _StubConverter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def convert(self, path):
            return types.SimpleNamespace(
                document=types.SimpleNamespace(
                    export_to_html=lambda: "<h1>标题</h1><p>内容</p>"
                )
            )

    stub = types.ModuleType("docling.document_converter")
    stub.DocumentConverter = _StubConverter
    stub.PdfFormatOption = _StubFormatOption
    monkeypatch.setitem(sys.modules, "docling.document_converter", stub)
    return captured


def _pdf_options(captured):
    from docling.datamodel.base_models import InputFormat

    fmt_opts = captured.get("format_options") or {}
    assert InputFormat.PDF in fmt_opts, "PDF format_options 未传入 converter"
    return fmt_opts[InputFormat.PDF].pipeline_options


class TestD3DoclingOptions:
    def test_default_fast_mode_and_ocr_auto(self, captured_converter):
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            TableFormerMode,
        )

        from app.document.docling_parser import DoclingParser

        converter = DoclingParser()._get_converter()
        assert converter is not None
        opts = _pdf_options(captured_converter)
        # 断言侧是真实 docling pydantic 模型，非替身镜像
        assert isinstance(opts, PdfPipelineOptions)
        assert opts.do_table_structure is True
        assert opts.table_structure_options.mode == TableFormerMode.FAST
        assert opts.do_ocr is True

    def test_accurate_mode_switch(self, captured_converter, monkeypatch):
        from docling.datamodel.pipeline_options import TableFormerMode

        from app.config import get_settings
        from app.document.docling_parser import DoclingParser

        monkeypatch.setattr(
            get_settings(), "DOCLING_TABLE_MODE", "ACCURATE", raising=False
        )
        DoclingParser()._get_converter()
        opts = _pdf_options(captured_converter)
        assert opts.table_structure_options.mode == TableFormerMode.ACCURATE

    def test_ocr_switch_off(self, captured_converter, monkeypatch):
        from app.config import get_settings
        from app.document.docling_parser import DoclingParser

        monkeypatch.setattr(get_settings(), "DOCLING_DO_OCR", False, raising=False)
        DoclingParser()._get_converter()
        opts = _pdf_options(captured_converter)
        assert opts.do_ocr is False

    def test_invalid_mode_falls_back_fast(self, captured_converter, monkeypatch):
        from docling.datamodel.pipeline_options import TableFormerMode

        from app.config import get_settings
        from app.document.docling_parser import DoclingParser

        monkeypatch.setattr(
            get_settings(), "DOCLING_TABLE_MODE", "bogus", raising=False
        )
        DoclingParser()._get_converter()
        opts = _pdf_options(captured_converter)
        assert opts.table_structure_options.mode == TableFormerMode.FAST

    def test_config_defaults_witness(self):
        from app.config import get_settings

        settings = get_settings()
        assert settings.DOCLING_TABLE_MODE == "FAST"
        assert settings.DOCLING_DO_OCR is True

    async def test_parse_wiring_witness(self, captured_converter):
        """正向证人：显式 options 不破坏 parse 输出链路。"""
        from app.document.docling_parser import DoclingParser

        html = await DoclingParser().parse("dummy.pdf")
        assert html == "<h1>标题</h1><p>内容</p>"
