"""D2 闸门 — TOC/书签真值接入 PDF/DOCX 标题层级。

红基线契约：PDF 书签（get_toc）零利用，产物无任何 <h> 层级；
DOCX 仅认 pStyle HeadingN，outlineLvl / TOC 域真值零利用。
修复后契约：
    - PDF 按书签页码注入 <h1>-<h6>，正文重复标题行去重；
    - DOCX outlineLvl 优先于 HeadingN 样式；TOC 域条目锚定无样式正文标题；
    - 无 TOC / 开关关闭时行为不回归（正向证人）。

真实边界：fitz 真实生成带 set_toc 的 PDF；python-docx 真实生成
outlineLvl / TOC 域 DOCX，走完整 parse() 管线。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（B10 教训：httpx trust_env 解析即炸）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


# ----------------------------------------------------------------------
# 真实样本构造
# ----------------------------------------------------------------------

_P1_BODY = "本章介绍项目背景与目标，覆盖范围包括解析管线与检索链路。"
_P2_BODY = "现有解析器对目录书签零利用，标题层级在分块阶段全部丢失。"
_P3_BODY = "资产化方案采用薄层模型，图片与表格作为可检索资产下发。"
_LONG_TITLE = "第二章 系统总体设计与技术选型说明（含架构演进路线与里程碑计划安排）"


def _make_pdf(tmp_path, pages, toc=None):
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontname="china-s", fontsize=12)
    if toc:
        doc.set_toc(toc)
    path = tmp_path / "d2.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def _set_pstyle(p, style_id):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    pPr = p._p.get_or_add_pPr()
    el = OxmlElement("w:pStyle")
    el.set(qn("w:val"), style_id)
    pPr.insert(0, el)


def _set_outline(p, level):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    pPr = p._p.get_or_add_pPr()
    el = OxmlElement("w:outlineLvl")
    el.set(qn("w:val"), str(level))
    pPr.append(el)


def _make_docx(tmp_path, specs):
    """specs: [{"text", "style"?, "outline"?, "toc_page"?}]"""
    from docx import Document

    doc = Document()
    for spec in specs:
        p = doc.add_paragraph(spec["text"])
        if "style" in spec:
            _set_pstyle(p, spec["style"])
        if "outline" in spec:
            _set_outline(p, spec["outline"])
        if "toc_page" in spec:
            p.add_run(f"\t{spec['toc_page']}")
    path = tmp_path / "d2.docx"
    doc.save(str(path))
    return str(path)


# ----------------------------------------------------------------------
# PDF 书签真值
# ----------------------------------------------------------------------


class TestPdfTocTruth:
    async def test_bookmarks_inject_headings(self, tmp_path):
        from app.document.pdf_parser import PDFParser

        path = _make_pdf(
            tmp_path,
            [
                f"第一章 概述\n{_P1_BODY}",
                f"1.1 背景\n{_P2_BODY}",
                f"第二章 设计\n{_P3_BODY}",
            ],
            toc=[[1, "第一章 概述", 1], [2, "1.1 背景", 2], [1, "第二章 设计", 3]],
        )
        out = await PDFParser().parse(path)
        assert "<h1>第一章 概述</h1>" in out
        assert "<h2>1.1 背景</h2>" in out
        assert "<h1>第二章 设计</h1>" in out
        # 层级顺序与书签/页码一致
        assert out.index("<h1>第一章 概述</h1>") < out.index(_P1_BODY)
        assert out.index(_P1_BODY) < out.index("<h2>1.1 背景</h2>")
        assert out.index("<h2>1.1 背景</h2>") < out.index("<h1>第二章 设计</h1>")

    async def test_toc_title_deduped_from_body(self, tmp_path):
        from app.document.pdf_parser import PDFParser

        path = _make_pdf(
            tmp_path,
            [f"第一章 概述\n{_P1_BODY}"],
            toc=[[1, "第一章 概述", 1]],
        )
        out = await PDFParser().parse(path)
        assert "<h1>第一章 概述</h1>" in out
        # 标题只出现一次（注入的 h 标签），正文重复行被剔除
        assert out.count("第一章 概述") == 1
        assert _P1_BODY in out

    async def test_no_toc_behavior_unchanged(self, tmp_path):
        """正向证人：无书签 PDF 不产标题，正文完整。"""
        from app.document.pdf_parser import PDFParser

        path = _make_pdf(tmp_path, [f"第一章 概述\n{_P1_BODY}"])
        out = await PDFParser().parse(path)
        assert "<h" not in out
        assert "第一章 概述" in out
        assert _P1_BODY in out

    async def test_toc_switch_off(self, tmp_path, monkeypatch):
        from app.config import get_settings
        from app.document.pdf_parser import PDFParser

        monkeypatch.setattr(
            get_settings(), "PDF_TOC_HEADINGS_ENABLED", False, raising=False
        )
        path = _make_pdf(
            tmp_path,
            [f"第一章 概述\n{_P1_BODY}"],
            toc=[[1, "第一章 概述", 1]],
        )
        out = await PDFParser().parse(path)
        assert "<h" not in out
        assert "第一章 概述" in out

    async def test_heading_only_page_not_misjudged_scan(self, tmp_path, monkeypatch):
        """仅含长标题的章节页：标题字符计入扫描判定，不误触发 OCR。"""
        from app.document.pdf_parser import PDFParser

        path = _make_pdf(
            tmp_path,
            [f"第一章 概述\n{_P1_BODY}", _LONG_TITLE],
            toc=[[1, "第一章 概述", 1], [1, _LONG_TITLE, 2]],
        )
        parser = PDFParser()
        ocr_calls: list[int] = []

        async def _fake_ocr(doc, page, page_num):
            ocr_calls.append(page_num)
            return ""

        monkeypatch.setattr(parser, "_scan_page_ocr", _fake_ocr)
        out = await parser.parse(path)
        assert 1 not in ocr_calls
        assert f"<h1>{_LONG_TITLE}</h1>" in out


# ----------------------------------------------------------------------
# DOCX outlineLvl / TOC 域真值
# ----------------------------------------------------------------------


class TestDocxTocTruth:
    async def test_outline_lvl_produces_headings(self, tmp_path):
        from app.document.docx_parser import DOCXParser

        path = _make_docx(
            tmp_path,
            [
                {"text": "第一章 概述", "outline": 0},
                {"text": _P1_BODY},
                {"text": "1.1 背景", "outline": 1},
                {"text": _P2_BODY},
            ],
        )
        out = await DOCXParser().parse(path)
        assert "<h1>第一章 概述</h1>" in out
        assert "<h2>1.1 背景</h2>" in out
        assert _P1_BODY in out

    async def test_outline_overrides_heading_style(self, tmp_path):
        """outlineLvl 是真值，HeadingN 样式降为补充。"""
        from app.document.docx_parser import DOCXParser

        path = _make_docx(
            tmp_path,
            [{"text": "第一章 概述", "style": "Heading3", "outline": 0}],
        )
        out = await DOCXParser().parse(path)
        assert "<h1>第一章 概述</h1>" in out
        assert "<h3>" not in out

    async def test_toc_entries_anchor_plain_body(self, tmp_path):
        """TOC 域条目（含制表符页码）锚定无样式正文标题；目录页本身不产标题。"""
        from app.document.docx_parser import DOCXParser

        path = _make_docx(
            tmp_path,
            [
                {"text": "第一章 概述", "style": "TOC1", "toc_page": 1},
                {"text": "1.1 背景", "style": "TOC2", "toc_page": 2},
                {"text": "第一章 概述"},
                {"text": _P1_BODY},
                {"text": "1.1 背景"},
                {"text": _P2_BODY},
            ],
        )
        out = await DOCXParser().parse(path)
        assert out.count("<h1>第一章 概述</h1>") == 1
        assert out.count("<h2>1.1 背景</h2>") == 1
        # 目录页条目（文本+页码）在前，正文锚定标题在后
        assert out.index("第一章 概述1") < out.index("<h1>第一章 概述</h1>")
        assert _P1_BODY in out and _P2_BODY in out

    async def test_heading_style_witness(self, tmp_path):
        """正向证人：既有 HeadingN 样式映射不回归。"""
        from app.document.docx_parser import DOCXParser

        path = _make_docx(
            tmp_path,
            [{"text": "第一章 概述", "style": "Heading1"}, {"text": _P1_BODY}],
        )
        out = await DOCXParser().parse(path)
        assert "<h1>第一章 概述</h1>" in out

    async def test_plain_doc_no_headings_witness(self, tmp_path):
        """正向证人：纯正文文档不凭空产标题。"""
        from app.document.docx_parser import DOCXParser

        path = _make_docx(tmp_path, [{"text": _P1_BODY}, {"text": _P2_BODY}])
        out = await DOCXParser().parse(path)
        assert "<h" not in out
        assert _P1_BODY in out

    async def test_outline_switch_off(self, tmp_path, monkeypatch):
        from app.config import get_settings
        from app.document.docx_parser import DOCXParser

        monkeypatch.setattr(
            get_settings(), "DOCX_OUTLINE_TRUTH_ENABLED", False, raising=False
        )
        path = _make_docx(
            tmp_path,
            [
                {"text": "第一章 概述", "outline": 0},
                {"text": "样式标题", "style": "Heading1"},
            ],
        )
        out = await DOCXParser().parse(path)
        assert "<h1>第一章 概述</h1>" not in out
        assert "第一章 概述" in out
        # 开关只关真值层，样式映射仍在
        assert "<h1>样式标题</h1>" in out
