"""D1 闸门 — VLM 失败不得以描述文本嵌入正文。

红基线契约：provider 失败返回「[图像理解失败: …]」字符串，被解析器当有效
描述嵌入正文/OCR 文本。修复后契约：understand 失败返回 None；解析器跳过
描述并记 last_parse_warnings；结构化路径标 status=error 且描述为空。

真实边界：AnthropicVisionProvider 走真实 understand 代码（LLM 替身抛错），
VLLMVisionProvider 走真实 understand 代码（client 替身抛错），
PDFParser.parse 用真实 fitz 生成含图 PDF 端到端。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（B10 教训：httpx trust_env 解析即炸）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


class _RaisingLLM:
    """LLM 替身 — chat 迭代即抛错，模拟 VLM 后端故障。"""

    async def chat(self, messages, stream=True, max_tokens=None):
        raise RuntimeError("vlm backend down")
        yield ""  # pragma: no cover — 使其成为异步生成器


class _NormalLLM:
    """LLM 替身 — 正常返回描述（正向证人）。"""

    async def chat(self, messages, stream=True, max_tokens=None):
        yield "这是一张测试图片的正常描述"


class _BoomCompletions:
    async def create(self, **kwargs):
        raise RuntimeError("vllm backend down")


def _boom_client():
    return SimpleNamespace(chat=SimpleNamespace(completions=_BoomCompletions()))


def _patch_vlm(monkeypatch, llm):
    from app.vlm import provider as vlm_provider

    monkeypatch.setattr(
        vlm_provider,
        "get_vision_provider",
        lambda: vlm_provider.AnthropicVisionProvider(llm=llm),
    )


class TestD1ProviderContract:
    async def test_anthropic_failure_returns_none(self):
        from app.vlm.provider import AnthropicVisionProvider

        p = AnthropicVisionProvider(llm=_RaisingLLM())
        out = await p.understand(b"img", "描述这张图片")
        assert out is None

    async def test_vllm_failure_returns_none(self):
        from app.vlm.provider import VLLMVisionProvider

        p = VLLMVisionProvider()
        p.client = _boom_client()
        out = await p.understand(b"img", "描述这张图片")
        assert out is None

    async def test_structured_failure_marks_error_without_failure_text(self):
        from app.vlm.provider import AnthropicVisionProvider

        p = AnthropicVisionProvider(llm=_RaisingLLM())
        d = await p.understand_structured(b"img", image_type="chart")
        assert d["status"] == "error"
        assert d["low_confidence"] is True
        assert "图像理解失败" not in (d.get("description") or "")


class TestD1ParserIsolation:
    async def test_pdf_describe_skips_and_warns(self, monkeypatch):
        from app.document.pdf_parser import PDFParser

        _patch_vlm(monkeypatch, _RaisingLLM())
        parser = PDFParser()
        out = await parser._vlm_describe(b"img", "image/png")
        assert out == ""
        assert any("描述" in w for w in parser.last_parse_warnings)

    async def test_docx_describe_skips_and_warns(self, monkeypatch):
        from app.document.docx_parser import DOCXParser

        _patch_vlm(monkeypatch, _RaisingLLM())
        parser = DOCXParser()
        out = await parser._vlm_describe(b"img", "image/png")
        assert out == ""
        assert any("描述" in w for w in parser.last_parse_warnings)

    async def test_pptx_describe_skips_and_warns(self, monkeypatch):
        from app.document.pptx_parser import PPTXParser

        _patch_vlm(monkeypatch, _RaisingLLM())
        parser = PPTXParser()
        out = await parser._vlm_describe(b"img", "image/png")
        assert out == ""
        assert any("描述" in w for w in parser.last_parse_warnings)

    async def test_pdf_scan_ocr_failure_returns_empty(self, monkeypatch):
        import fitz

        from app.document.pdf_parser import PDFParser

        _patch_vlm(monkeypatch, _RaisingLLM())
        doc = fitz.open()
        page = doc.new_page()
        try:
            parser = PDFParser()
            out = await parser._scan_page_ocr(doc, page, 0)
            assert out == ""
        finally:
            doc.close()

    async def test_normal_description_still_embedded(self, monkeypatch):
        """正向证人：VLM 正常时描述照常返回，不误伤。"""
        from app.document.pdf_parser import PDFParser

        _patch_vlm(monkeypatch, _NormalLLM())
        parser = PDFParser()
        out = await parser._vlm_describe(b"img", "image/png")
        assert out == "这是一张测试图片的正常描述"
        assert parser.last_parse_warnings == []


class TestD1EndToEnd:
    async def test_pdf_parse_with_failing_vlm_has_no_failure_text(
        self, monkeypatch, tmp_path
    ):
        """真实 fitz 含图 PDF + 故障 VLM → 产物无失败文案、warning 留痕。"""
        import fitz

        from app.document.pdf_parser import PDFParser

        _patch_vlm(monkeypatch, _RaisingLLM())

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            "这是一页用于测试的正文内容，长度超过三十个字符以避免触发扫描页判定。",
            fontname="china-s",
        )
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200))
        page.insert_image(fitz.Rect(72, 120, 272, 320), pixmap=pix)
        pdf_path = tmp_path / "d1_sample.pdf"
        doc.save(str(pdf_path))
        doc.close()

        parser = PDFParser()
        html = await parser.parse(str(pdf_path))
        assert "图像理解失败" not in html
        assert any("描述" in w for w in parser.last_parse_warnings)
