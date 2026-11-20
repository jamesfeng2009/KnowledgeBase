"""D8 闸门 — 解析引擎/降级链/耗时/OCR 页数落库（Document.parse_meta）。

红基线契约：parser_type 仅在日志，Document 无 parse_meta 列，
「最终走了哪条引擎、是否降级」不可回溯。修复后契约：
    - parse_meta.chain 记录每级引擎与结果（docling→legacy 两级链路）；
    - engine=最终引擎，duration_ms 耗时，ocr_pages 扫描 OCR 页数；
    - 哈希未变早退保留旧 meta；文件变化重解析刷新 meta（A14 重索引）。

真实边界：真实 fitz PDF 走真实 legacy PDFParser；Docling 边界用替身
（本机 torch 库缺，真实 Docling 不可用，见 task.md 环境勘误）。
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（B10 教训：httpx trust_env 解析即炸）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


@pytest.fixture(autouse=True)
def _cross_modal_off(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "CROSS_MODAL_ENABLED", False, raising=False)


_PDF_TEXT = "第一章 概述\n本章介绍项目背景与目标，覆盖范围包括解析管线与检索链路。"


def _make_pdf(tmp_path, scanned=False):
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    if scanned:
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200))
        pix.clear_with(90)
        page.insert_image(fitz.Rect(72, 72, 272, 272), pixmap=pix)
    else:
        page.insert_text((72, 72), _PDF_TEXT, fontname="china-s", fontsize=12)
    path = tmp_path / "d8.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def _make_doc(path, **kw):
    base = dict(
        id=uuid.uuid4(),
        file_path=path,
        doc_type="pdf",
        content_text=None,
        content_html=None,
        file_hash=None,
        page_count=0,
        parse_meta=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _stub_docling(monkeypatch, behavior):
    """替身 Docling 解析器：behavior 为返回字符串或抛出的异常。

    同时关闭 factory 的 Docling 可用性判定 — 否则 legacy 层 get_parser
    仍会路由到 DoclingParser（本机包可导入但 converter 断，见环境勘误）。
    """
    from app.document import factory

    class _StubDocling:
        async def parse(self, file_path):
            if isinstance(behavior, Exception):
                raise behavior
            return behavior

    monkeypatch.setattr(
        factory, "get_parser_with_fallback", lambda dt: (_StubDocling(), "docling")
    )
    monkeypatch.setattr(factory, "_is_docling_enabled", lambda: False)


class TestD8ParseMeta:
    def test_model_has_parse_meta_column(self):
        from sqlalchemy.dialects.postgresql import JSONB

        from app.models.knowledge import Document

        assert "parse_meta" in Document.__table__.columns
        col = Document.__table__.columns["parse_meta"]
        assert isinstance(col.type, JSONB)
        assert col.nullable is True

    def test_migration_witness(self):
        versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
        hits = [
            p
            for p in versions.glob("*.py")
            if "parse_meta" in p.read_text(encoding="utf-8")
            and "documents" in p.read_text(encoding="utf-8")
        ]
        assert hits, "缺少 documents.parse_meta 迁移脚本"
        body = hits[0].read_text(encoding="utf-8")
        assert "ADD COLUMN" in body and "JSONB" in body

    async def test_docling_empty_fallback_two_engine_chain(self, tmp_path, monkeypatch):
        from tasks.document_tasks import _parse_document

        _stub_docling(monkeypatch, "")
        doc = _make_doc(_make_pdf(tmp_path))
        text, _ = await _parse_document(doc)
        assert text.strip()
        meta = doc.parse_meta
        assert meta is not None
        chain = meta["chain"]
        assert [c["engine"] for c in chain] == ["docling", "legacy:pdf_enhanced"]
        assert [c["outcome"] for c in chain] == ["empty", "ok"]
        assert meta["engine"] == "legacy:pdf_enhanced"
        assert isinstance(meta["duration_ms"], int) and meta["duration_ms"] >= 0

    async def test_docling_error_recorded(self, tmp_path, monkeypatch):
        from tasks.document_tasks import _parse_document

        _stub_docling(monkeypatch, RuntimeError("docling backend down"))
        doc = _make_doc(_make_pdf(tmp_path))
        await _parse_document(doc)
        first = doc.parse_meta["chain"][0]
        assert first["engine"] == "docling"
        assert first["outcome"] == "error"
        assert "docling backend down" in first["error"]

    async def test_docling_success_single_engine_witness(self, tmp_path, monkeypatch):
        """正向证人：Docling 成功只记单引擎，不凭空造降级链。"""
        from tasks.document_tasks import _parse_document

        _stub_docling(monkeypatch, "<h1>标题</h1>正文内容足够长以避免扫描判定。")
        doc = _make_doc(_make_pdf(tmp_path))
        text, _ = await _parse_document(doc)
        assert "<h1>标题</h1>" in text
        meta = doc.parse_meta
        assert len(meta["chain"]) == 1
        assert meta["chain"][0] == {"engine": "docling", "outcome": "ok"}
        assert meta["engine"] == "docling"

    async def test_docling_unavailable_chain(self, tmp_path, monkeypatch):
        from app.document import factory
        from tasks.document_tasks import _parse_document

        monkeypatch.setattr(
            factory, "get_parser_with_fallback", lambda dt: (None, "legacy")
        )
        monkeypatch.setattr(factory, "_is_docling_enabled", lambda: False)
        doc = _make_doc(_make_pdf(tmp_path))
        await _parse_document(doc)
        chain = doc.parse_meta["chain"]
        assert chain[0] == {"engine": "docling", "outcome": "unavailable"}
        assert chain[-1]["engine"] == "legacy:pdf_enhanced"

    async def test_ocr_pages_recorded(self, tmp_path, monkeypatch):
        from app.document.pdf_parser import PDFParser
        from tasks.document_tasks import _parse_document

        _stub_docling(monkeypatch, "")

        async def _fake_ocr(self, doc, page, page_num):
            return "扫描页识别文字内容"

        monkeypatch.setattr(PDFParser, "_scan_page_ocr", _fake_ocr)
        doc = _make_doc(_make_pdf(tmp_path, scanned=True))
        await _parse_document(doc)
        assert doc.parse_meta["ocr_pages"] == 1

    async def test_hash_unchanged_keeps_meta(self, tmp_path):
        """哈希未变早退：不重解析，旧 meta 原样保留。"""
        from tasks.document_tasks import _parse_document

        path = _make_pdf(tmp_path)
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        old_meta = {"engine": "old", "chain": []}
        doc = _make_doc(
            path, content_text="旧文本", file_hash=digest, parse_meta=old_meta
        )
        text, _ = await _parse_document(doc)
        assert text == "旧文本"
        assert doc.parse_meta is old_meta

    async def test_reindex_refresh_on_file_change(self, tmp_path, monkeypatch):
        """A14 重索引：文件变化 → 重解析 → parse_meta 刷新。"""
        from tasks.document_tasks import _parse_document

        _stub_docling(monkeypatch, "")
        path = _make_pdf(tmp_path)
        doc = _make_doc(
            path,
            content_text="旧文本",
            file_hash="stale-hash",
            parse_meta={"engine": "old", "chain": []},
        )
        text, _ = await _parse_document(doc)
        assert text != "旧文本"
        assert doc.parse_meta["engine"] == "legacy:pdf_enhanced"
        assert doc.parse_meta["chain"]
        assert doc.file_hash != "stale-hash"

    async def test_video_chain_entry(self, tmp_path, monkeypatch):
        import tasks.document_tasks as dt

        async def _fake_video(doc):
            return "视频转写文本"

        monkeypatch.setattr(dt, "_parse_video", _fake_video)
        doc = _make_doc(str(tmp_path / "v.mp4"), doc_type="mp4")
        text, _ = await dt._parse_document(doc)
        assert text == "视频转写文本"
        assert doc.parse_meta["chain"][0]["engine"] == "video_pipeline"
        assert doc.parse_meta["engine"] == "video_pipeline"
